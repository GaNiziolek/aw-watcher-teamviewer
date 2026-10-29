"""ActivityWatch watcher for TeamViewer sessions.

Session list: TeamViewer's connection logs (the data behind TeamViewer's
"Connection log"; timestamps in these files are UTC):
  * outgoing: %APPDATA%\\TeamViewer\\Connections.txt
  * incoming: <TeamViewer install dir>\\Connections_incoming.txt

Optional enrichment (device name, group, notes): the official TeamViewer Web
API, `GET /api/v1/reports/connections`, when a script token is configured
(env var TEAMVIEWER_API_TOKEN or the token file, see README).

Events go to the bucket `aw-watcher-teamviewer_<hostname>`. Only the standard
library is needed.

Usage:  aw-watcher-teamviewer [--once] [--interval 60] [--port 5600]
        aw-watcher-teamviewer --check-api
"""
import argparse
import glob
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

CLIENT = "aw-watcher-teamviewer"
EVENT_TYPE = "teamviewer.session"
TIME_FORMAT = "%d-%m-%Y %H:%M:%S"
TV_API = "https://webapi.teamviewer.com/api/v1"
RETRY_WINDOW = timedelta(hours=24)  # API reports may lag; retry recent sessions
ARRAY_SPLIT = re.compile(r"\t|\s{2,}")


def token_file():
    base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    return os.path.join(base, "activitywatch", "activitywatch", CLIENT, "token.txt")


def load_token():
    token = os.environ.get("TEAMVIEWER_API_TOKEN", "").strip()
    if token:
        return token
    try:
        with open(token_file(), encoding="utf-8-sig") as f:
            return f.read().strip()
    except OSError:
        return ""


def log_files():
    """Yield (direction, path) for every TeamViewer connection log found."""
    candidates = []
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        candidates.append(("outgoing", os.path.join(appdata, "TeamViewer", "Connections.txt")))
    roots = [os.environ.get("ProgramFiles", r"C:\Program Files"),
             os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")]
    for root in roots:
        candidates.append(("incoming", os.path.join(root, "TeamViewer", "Connections_incoming.txt")))
    # Some installs keep both logs in one place; accept either name anywhere.
    for root in roots:
        for path in glob.glob(os.path.join(root, "TeamViewer", "Connections*.txt")):
            candidates.append(("incoming" if "incoming" in path.lower() else "outgoing", path))
    seen = set()
    for direction, path in candidates:
        key = os.path.normcase(path)
        if key not in seen:
            seen.add(key)
            yield direction, path


def digits(value):
    return re.sub(r"\D", "", value or "")


def norm_guid(value):
    return (value or "").strip().strip("{}").lower()


def parse_log(direction, path):
    """Parse one log file into session dicts. Columns: partner id, partner
    name (incoming only), start, end, local user, session type, guid."""
    sessions = []
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return sessions
    for line in lines:
        # Incoming log is tab separated, outgoing is padded with runs of
        # spaces; single spaces occur inside names and timestamps.
        cols = [c.strip() for c in ARRAY_SPLIT.split(line)]
        cols = [c for c in cols if c != ""]
        if len(cols) == 6:
            cols.insert(1, "")
        if len(cols) < 7:
            continue
        pid, name, start, end, user, kind, guid = cols[:7]
        try:
            start_dt = datetime.strptime(start, TIME_FORMAT).replace(tzinfo=timezone.utc)
            end_dt = datetime.strptime(end, TIME_FORMAT).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        sessions.append({
            "start": start_dt,
            "duration": max((end_dt - start_dt).total_seconds(), 0.0),
            "data": {
                "direction": direction,
                "device_id": digits(pid),
                "device_name": name,
                "group_name": "",
                "notes": "",
                "local_user": user,
                "session_type": kind,
                "guid": norm_guid(guid),
            },
        })
    return sessions


# --- TeamViewer Web API ----------------------------------------------------

def tv_get(token, path, params):
    url = f"{TV_API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def fetch_reports(token, start, end):
    """All connection reports in [start, end), following the paging link."""
    params = {"from_date": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "to_date": end.strftime("%Y-%m-%dT%H:%M:%SZ")}
    records = []
    while True:
        page = tv_get(token, "/reports/connections", params)
        got = page.get("records", [])
        records += got
        if not got or not page.get("records_remaining", 0):
            return records
        params["offset_id"] = page.get("next_offset") or got[-1].get("id")


def parse_api_time(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def find_report(reports, data, start):
    guid = data["guid"]
    for r in reports:
        if norm_guid(r.get("id")) == guid or norm_guid(r.get("session_code")) == guid:
            return r
    for r in reports:  # fall back to same device + start time within 2 minutes
        r_start = parse_api_time(r.get("start_date"))
        if (r_start and digits(r.get("deviceid")) == data["device_id"]
                and abs((r_start - start).total_seconds()) < 120):
            return r
    return None


def enrich(data, report):
    if report.get("devicename"):
        data["device_name"] = report["devicename"]
    data["group_name"] = report.get("groupname") or ""
    data["notes"] = report.get("notes") or ""


# --- ActivityWatch ---------------------------------------------------------

def api(base, method, path, body=None):
    req = urllib.request.Request(
        base + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


def ensure_bucket(base, bucket_id, hostname):
    """Create the bucket; recreate it if it holds events of the old schema.

    (aw-server does not persist bucket `data`, so the schema is detected from
    the events themselves: version 2 events carry `device_id`.)
    """
    try:
        api(base, "GET", f"/buckets/{bucket_id}")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    else:
        sample = api(base, "GET", f"/buckets/{bucket_id}/events?limit=1")
        if not sample or "device_id" in sample[0]["data"]:
            return
        api(base, "DELETE", f"/buckets/{bucket_id}?force=1")
    api(base, "POST", f"/buckets/{bucket_id}",
        {"client": CLIENT, "type": EVENT_TYPE, "hostname": hostname})


def sync(base, bucket_id, token):
    existing = api(base, "GET", f"/buckets/{bucket_id}/events?limit=-1") or []
    known = {e["data"].get("guid"): e for e in existing}
    new, seen = [], set()
    for direction, path in log_files():
        for s in parse_log(direction, path):
            guid = s["data"]["guid"]
            if guid in known or guid in seen:
                continue
            seen.add(guid)
            new.append({"timestamp": s["start"].isoformat(),
                        "duration": s["duration"], "data": s["data"]})

    updated = []
    if token:
        now = datetime.now(timezone.utc)
        todo = [e for e in new] + [e for e in existing if not e["data"].get("api_checked")]
        if todo:
            starts = [parse_api_time(e["timestamp"]) for e in todo]
            lo = min(starts) - timedelta(days=1)
            try:
                reports = fetch_reports(token, lo, now + timedelta(days=1))
            except urllib.error.HTTPError as e:
                print(f"TeamViewer API error {e.code} ({e.reason}); "
                      f"check the token and its 'connection reports' permission", flush=True)
                reports = None
            if reports is not None:
                for e in todo:
                    start = parse_api_time(e["timestamp"])
                    report = find_report(reports, e["data"], start)
                    if report:
                        enrich(e["data"], report)
                    if report or now - (start + timedelta(seconds=e["duration"])) > RETRY_WINDOW:
                        e["data"]["api_checked"] = True
                    if e in existing:
                        updated.append(e)

    batch = new + updated  # events carrying an `id` replace the stored ones
    for i in range(0, len(batch), 500):
        api(base, "POST", f"/buckets/{bucket_id}/events", batch[i:i + 500])
    return len(new), len(updated)


def check_api():
    token = load_token()
    if not token:
        print(f"No token found. Set TEAMVIEWER_API_TOKEN or put it in {token_file()}")
        raise SystemExit(1)
    now = datetime.now(timezone.utc)
    try:
        reports = fetch_reports(token, now - timedelta(days=30), now + timedelta(days=1))
    except urllib.error.HTTPError as e:
        print(f"API error {e.code} {e.reason}: {e.read()[:300]!r}")
        raise SystemExit(1)
    print(f"{len(reports)} report(s) in the last 30 days")
    if reports:
        print("fields:", ", ".join(sorted(reports[0])))
        print(json.dumps(reports[0], indent=2)[:1500])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true", help="sync once and exit")
    p.add_argument("--interval", type=int, default=60, help="poll seconds")
    p.add_argument("--port", type=int, default=5600)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--check-api", action="store_true",
                   help="test the TeamViewer API token and print a sample report")
    a = p.parse_args()
    if a.check_api:
        return check_api()

    base = f"http://{a.host}:{a.port}/api/0"
    hostname = socket.gethostname()
    bucket_id = f"{CLIENT}_{hostname}"
    while True:
        try:
            ensure_bucket(base, bucket_id, hostname)
            n_new, n_upd = sync(base, bucket_id, load_token())
            if n_new or n_upd:
                print(f"{datetime.now():%H:%M:%S} imported {n_new}, enriched {n_upd}", flush=True)
        except (urllib.error.URLError, OSError) as e:
            print(f"ActivityWatch unreachable: {e}", flush=True)
            if a.once:
                raise SystemExit(1)
        if a.once:
            return
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
