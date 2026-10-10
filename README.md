# me_server_manager

A small web dashboard for the Raspberry Pi that builds, starts, stops and monitors the
Village Shop game server and content server. One Python file, standard library only.
It is meant for your local network only: there is no login.

## What the page shows
- **System**: CPU %, RAM, CPU temperature, Pi power/throttling warnings, network traffic,
  load, free disk space, uptime, plus history charts (CPU, RAM, temperature, network) for
  the last 15 min / 1 h / 6 h. History is kept in memory and cleared when the dashboard restarts.
- **One section per server**: running or stopped, Start (when stopped) or Stop / Restart
  (when running), PID, uptime, RAM, CPU, the last crash (if any), when the binary was built,
  which commit is running, a "restart needed" note after a rebuild, and the last lines of the log.
- **Build**: *Build & Run* (pull, build, then restart both servers), *Build only*, or
  *Clean Build & Run* (wipe compiled files first, then the same as Build & Run), with the
  build state, commit, duration and output.

The page refreshes every 5 seconds. Click *full log* for a longer log that doesn't refresh.
Layout (vertical / horizontal) and chart range are remembered per browser.

## Crashes and auto-restart
If a server stops without being stopped from the dashboard, its section shows the time and
reason (e.g. `killed by SIGSEGV`), and a line is added to its log. With `"auto_restart": true`
it is started again; after 3 crashes in 10 minutes it gives up so a crash loop doesn't spin
forever. Pressing Start resets that. A server you stop by hand over SSH also counts as a crash.

## Power / throttling
On a Pi, the dashboard reads the firmware's throttle flags. *NOW* (red) means the Pi is
under-voltage or throttled right now; *since boot* (orange) means it happened at some point.
Under-voltage almost always means the power supply is too weak.

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
| `clean_steps` | Run before `build_steps` for a clean build. Uses CMake's `clean` target rather than deleting `build/`, so the game server's `village_shop.db` (high score, next to the binary) survives. |
| `servers` | `name`, `exe` (relative to `repo_dir`), optional `args` list, `autostart`, `auto_restart`. |

## Logs
Logs are kept in `logs/` next to the script:
- `<server name>.log` holds the output of each server the dashboard started.
- `build.log` holds the output of the last build; `build_info.json` its result and commit.

`logs/` is in `.gitignore`, so server output never ends up in the repo.

A server log is renamed to `.old` once it grows past 5 MB. If you start a server by hand
over SSH, the dashboard still shows it as running and can stop it, but its output goes
to your terminal, not the log.
