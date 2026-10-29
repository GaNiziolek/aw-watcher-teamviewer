"""ActivityWatch watcher for TeamViewer sessions.

Reads TeamViewer's official connection logs (the same data shown under
TeamViewer > Extras > "Connection log"):
  * outgoing: %APPDATA%\\TeamViewer\\Connections.txt
  * incoming: <TeamViewer install dir>\\Connections_incoming.txt
and writes each finished session as an event to the bucket
`aw-watcher-teamviewer_<hostname>`. Only the standard library is needed.

Usage:  python aw_watcher_teamviewer.py [--once] [--interval 60] [--port 5600]
"""
import argparse
import glob
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime

CLIENT = "aw-watcher-teamviewer"
EVENT_TYPE = "teamviewer.session"
TIME_FORMAT = "%d-%m-%Y %H:%M:%S"  # local time, as written by TeamViewer


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


def parse_log(direction, path):
    """Parse one log file into session dicts. Columns are tab separated:
    partner id, partner name, start, end, local user, session type, guid."""
    sessions = []
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return sessions
    for line in lines:
        # The incoming log is tab separated, the outgoing one is padded with
        # runs of spaces; single spaces occur inside names and timestamps.
        cols = [c.strip() for c in re.split(r"\t|\s{2,}", line)]
        cols = [c for c in cols if c != ""]
        # The outgoing log omits the partner name when it is unknown.
        if len(cols) == 6:
            cols.insert(1, "")
        if len(cols) < 7:
            continue
        pid, name, start, end, user, kind, guid = cols[:7]
        try:
            start_dt = datetime.strptime(start, TIME_FORMAT).astimezone()
            end_dt = datetime.strptime(end, TIME_FORMAT).astimezone()
        except ValueError:
            continue
        sessions.append({
            "start": start_dt,
            "duration": max((end_dt - start_dt).total_seconds(), 0.0),
            "data": {
                "direction": direction,
                "partner_id": pid,
                "partner_name": name,
                "local_user": user,
                "session_type": kind,
                "guid": guid.strip("{}"),
            },
        })
    return sessions


def api(base, method, path, body=None):
    req = urllib.request.Request(
        base + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


def ensure_bucket(base, bucket_id, hostname):
    try:
        api(base, "POST", f"/buckets/{bucket_id}",
            {"client": CLIENT, "type": EVENT_TYPE, "hostname": hostname})
    except urllib.error.HTTPError as e:
        if e.code != 304:  # 304 = bucket already exists
            raise


def sync(base, bucket_id):
    known = {e["data"].get("guid") for e in
             api(base, "GET", f"/buckets/{bucket_id}/events?limit=-1") or []}
    seen, new = set(), []
    for direction, path in log_files():
        for s in parse_log(direction, path):
            guid = s["data"]["guid"]
            if guid in known or guid in seen:
                continue
            seen.add(guid)
            new.append({"timestamp": s["start"].isoformat(),
                        "duration": s["duration"], "data": s["data"]})
    if new:
        api(base, "POST", f"/buckets/{bucket_id}/events", new)
    return len(new)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true", help="sync once and exit")
    p.add_argument("--interval", type=int, default=60, help="poll seconds")
    p.add_argument("--port", type=int, default=5600)
    p.add_argument("--host", default="127.0.0.1")
    a = p.parse_args()

    base = f"http://{a.host}:{a.port}/api/0"
    hostname = socket.gethostname()
    bucket_id = f"{CLIENT}_{hostname}"
    while True:
        try:
            ensure_bucket(base, bucket_id, hostname)
            n = sync(base, bucket_id)
            if n:
                print(f"{datetime.now():%H:%M:%S} imported {n} session(s)", flush=True)
        except (urllib.error.URLError, OSError) as e:
            print(f"ActivityWatch unreachable: {e}", flush=True)
            if a.once:
                raise SystemExit(1)
        if a.once:
            return
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
