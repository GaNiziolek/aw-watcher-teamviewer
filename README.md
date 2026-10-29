# aw-watcher-teamviewer

A [watcher](https://docs.activitywatch.net/en/latest/watchers.html) for [ActivityWatch](https://activitywatch.net) that records your **TeamViewer sessions** (incoming and outgoing) as events, next to the rest of your activity data.

It reads TeamViewer's own connection logs, the same data shown in TeamViewer's *Connection log*, so no API token or licence is needed. It has no dependencies beyond the Python standard library.

| Direction | Log file |
|-----------|----------|
| outgoing  | `%APPDATA%\TeamViewer\Connections.txt` |
| incoming  | `C:\Program Files\TeamViewer\Connections_incoming.txt` |

> **Note:** TeamViewer writes a log line only when a session *ends*, so a session appears in ActivityWatch after it finishes, not while it is running.

## Event format

Bucket: `aw-watcher-teamviewer_<hostname>` (event type `teamviewer.session`)

```json
{
  "timestamp": "2026-01-15T14:30:00+00:00",
  "duration": 300.0,
  "data": {
    "direction": "incoming",
    "partner_id": "123456789",
    "partner_name": "Jane Doe",
    "local_user": "John Smith",
    "session_type": "RemoteControl",
    "guid": "00000000-0000-0000-0000-000000000000"
  }
}
```

Sessions are de-duplicated by `guid`, so re-running or restarting never creates duplicates.

## Installation

Requirements: Windows, ActivityWatch (`aw-server` running), TeamViewer, Python 3.8+.

1. **Install the package.** This puts an `aw-watcher-teamviewer` executable in your Python `Scripts` folder:

   ```bash
   git clone <this-repo-url> aw-watcher-teamviewer
   pip install ./aw-watcher-teamviewer
   ```

   Make sure the Python `Scripts` directory is on your `PATH`. Check with:

   ```bash
   where aw-watcher-teamviewer
   ```

   If it prints nothing, pip probably installed into your user folder and warned `... is not on PATH`
   (typically `%APPDATA%\Python\Python313\Scripts`). Add that folder to your user `PATH`
   (PowerShell, adjust the Python version):

   ```powershell
   $s = "$env:APPDATA\Python\Python313\Scripts"
   [Environment]::SetEnvironmentVariable("Path", [Environment]::GetEnvironmentVariable("Path","User").TrimEnd(';') + ";" + $s, "User")
   ```

   **Sign out and back in** (or reboot) so that aw-qt, which is started by Windows at logon, inherits the new `PATH`.
   Restarting aw-qt from a terminal that was opened before the change will not work.

2. **Let ActivityWatch manage it.** `aw-qt` (the ActivityWatch tray app) finds modules named `aw-*` on your `PATH` and starts the ones listed in `autostart_modules` in its config file:

   `%LOCALAPPDATA%\activitywatch\activitywatch\aw-qt\aw-qt.toml`

   Uncomment `autostart_modules` and add the watcher:

   ```toml
   [aw-qt]
   autostart_modules = ["aw-server", "aw-watcher-afk", "aw-watcher-window", "aw-watcher-teamviewer"]
   ```

   Keep the other modules you already use in the list. Then quit and restart ActivityWatch from the tray icon. The watcher is also listed in the tray menu under *Modules*, where you can start or stop it.

3. **Verify.** Open the ActivityWatch web UI at <http://localhost:5600> and check **Buckets** for `aw-watcher-teamviewer_<hostname>`, or use the **Timeline** view.

### Run manually (without aw-qt)

```bash
aw-watcher-teamviewer              # runs continuously, polling every 60 s
aw-watcher-teamviewer --once       # sync once and exit
aw-watcher-teamviewer --interval 30 --port 5600 --host 127.0.0.1
```

## Using the data

- **Categorization:** in the web UI go to *Settings → Categorization* and add a rule matching the bucket data, for example regex `TeamViewer` on the app name, or create categories for `direction` / `partner_name`.
- **Query:** in the *Query Explorer* you can use `query_bucket(find_bucket("aw-watcher-teamviewer_"))`.

## Troubleshooting

- `Module aw-watcher-teamviewer not found` in the aw-qt log (`%LOCALAPPDATA%\activitywatch\activitywatch\Logs\aw-qt`): aw-qt could not see the executable on its `PATH` (`Found 0 system modules`). See the `PATH` step above.
- `ActivityWatch unreachable`: `aw-server` isn't running or listens on another port (`--port`).
- No events: check that the log files above exist. TeamViewer must have *Connection log* enabled (default). If you installed TeamViewer to a custom location, the incoming log is expected under `Program Files\TeamViewer`.
- Dates are read as local time in `dd-mm-yyyy hh:mm:ss`, as TeamViewer writes them.

## License

MIT
