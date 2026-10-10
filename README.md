# me_server_manager

A small web dashboard for the Raspberry Pi that builds, starts, stops and monitors the
Village Shop game server and content server. One Python file, standard library only.
It is meant for your local network only: there is no login.

## What the page shows
- **System**: CPU %, RAM, CPU temperature, load, free disk space, uptime.
- **One section per server**: running or stopped, Start (when stopped) or Stop / Restart
  (when running), PID, uptime, RAM, CPU, and the last lines of the server's log.
- **Build**: *Build & Run* (pull, build, then restart both servers) or *Build only*,
  with the build output.

The page refreshes every 5 seconds. Click *full log* for a longer log that doesn't refresh.

## Setup on the Pi
1. Copy or clone this folder to the Pi, e.g. `/home/amrit/gamedev/me_server_manager/me_server_manager`.
2. Edit `config.json`: set `repo_dir` to your `village_shop` clone on the Pi.
3. Run it: `python3 server_manager.py`
4. Open `http://<pi-ip>:8080` from any machine on your network.

### Start at boot (optional)
Edit `User` and the paths in `server-manager.service`, then:
```
sudo cp server-manager.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now server-manager
```
Restarting the dashboard doesn't stop the game servers. To start a server automatically
when the dashboard starts (e.g. after a reboot), set `"autostart": true` for it in `config.json`.

## config.json
| Key | Meaning |
|---|---|
| `repo_dir` | Path to the `village_shop` repo on the Pi. |
| `host`, `port` | Where the dashboard listens (default `0.0.0.0:8080`). |
| `build_steps` | Shell commands run in order in `repo_dir`; the build stops at the first failure. |
| `servers` | `name`, `exe` (relative to `repo_dir`), optional `args` list, `autostart`. |

## Logs
Logs are kept in `logs/` next to the script:
- `<server name>.log` holds the output of each server the dashboard started.
- `build.log` holds the output of the last build.

A server log is renamed to `.old` once it grows past 5 MB. If you start a server by hand
over SSH, the dashboard still shows it as running and can stop it, but its output goes
to your terminal, not the log.
