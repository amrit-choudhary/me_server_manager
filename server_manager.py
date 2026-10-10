#!/usr/bin/env python3
"""Small web dashboard to build, start, stop and monitor the game servers on the Pi.

Standard library only. Run: python3 server_manager.py [config.json]
"""

import html
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")
BUILD_LOG = os.path.join(LOG_DIR, "build.log")
MAX_LOG_BYTES = 5 * 1024 * 1024
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

config = {}
children = []  # Popen objects we started, kept so exited ones get reaped.
action_lock = threading.Lock()  # One start/stop/build action at a time.
message = {"text": "", "time": 0.0}
build_state = {"state": "not run yet", "started": None, "finished": None}
cpu_stats = {"system": None, "procs": {}}  # Filled by the sampler thread.


# ---------------------------------------------------------------- helpers

def load_config(path):
    with open(path) as f:
        cfg = json.load(f)
    cfg["repo_dir"] = os.path.realpath(os.path.expanduser(cfg["repo_dir"]))
    for server in cfg["servers"]:
        server["exe"] = os.path.realpath(os.path.join(cfg["repo_dir"], server["exe"]))
    return cfg


def set_message(text):
    message["text"] = text
    message["time"] = time.time()


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def format_duration(seconds):
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {seconds}s"


def read_file(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def tail(path, lines):
    """Last `lines` lines of a text file, with terminal colour codes removed."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 256 * 1024))
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return "(no log yet)"
    return "\n".join(ANSI_ESCAPE.sub("", data).splitlines()[-lines:])


def server_log_path(server):
    return os.path.join(LOG_DIR, server["name"] + ".log")


def find_server(name):
    for server in config["servers"]:
        if server["name"] == name:
            return server
    return None


# ---------------------------------------------------------------- processes

def reap_children():
    for proc in children[:]:
        if proc.poll() is not None:
            children.remove(proc)


def find_pids(exe):
    """PIDs whose executable is `exe`, including ones started by hand over SSH."""
    reap_children()
    if not os.path.isdir("/proc"):
        return []
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            target = os.readlink(f"/proc/{entry}/exe")
        except OSError:
            continue
        # A rebuilt binary shows up as "<path> (deleted)" for the old running process.
        if target.removesuffix(" (deleted)") == exe:
            pids.append(int(entry))
    return pids


def process_info(pid):
    """RAM (MB) and uptime (seconds) of a process, or None if it is gone."""
    status = read_file(f"/proc/{pid}/status")
    stat = read_file(f"/proc/{pid}/stat")
    uptime = read_file("/proc/uptime")
    if not status or not stat or not uptime:
        return None
    rss_kb = 0
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            rss_kb = int(line.split()[1])
    fields = stat.rsplit(")", 1)[1].split()
    start_ticks = int(fields[19])
    age = float(uptime.split()[0]) - start_ticks / os.sysconf("SC_CLK_TCK")
    return {"ram_mb": rss_kb / 1024, "uptime": age}


def start_server(server):
    if find_pids(server["exe"]):
        return f"{server['name']} is already running."
    if not os.path.isfile(server["exe"]):
        return f"{server['name']} is not built yet ({server['exe']} missing)."

    log_path = server_log_path(server)
    if os.path.isfile(log_path) and os.path.getsize(log_path) > MAX_LOG_BYTES:
        os.replace(log_path, log_path + ".old")

    cmd = [server["exe"]] + server.get("args", [])
    if shutil.which("stdbuf"):
        cmd = ["stdbuf", "-oL", "-eL"] + cmd  # Line-buffer output so the log updates live.

    with open(log_path, "ab") as log:
        log.write(f"\n===== started {now_str()} =====\n".encode())
        log.flush()
        proc = subprocess.Popen(cmd, cwd=os.path.dirname(server["exe"]), stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    children.append(proc)

    time.sleep(1.0)
    if proc.poll() is not None:
        return f"{server['name']} exited right away (code {proc.returncode}). Check its log."
    return f"{server['name']} started (pid {proc.pid})."


def stop_server(server):
    pids = find_pids(server["exe"])
    if not pids:
        return f"{server['name']} is not running."
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + 5
    while time.time() < deadline and find_pids(server["exe"]):
        time.sleep(0.2)
    for pid in find_pids(server["exe"]):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    time.sleep(0.2)
    if find_pids(server["exe"]):
        return f"Could not stop {server['name']}."
    return f"{server['name']} stopped."


def restart_server(server):
    stop_message = stop_server(server)
    return stop_message + " " + start_server(server)


# ---------------------------------------------------------------- build

def run_build(then_run):
    """Runs the configured build steps in order, stopping at the first failure."""
    build_state.update(state="running", started=now_str(), finished=None)
    ok = True
    with open(BUILD_LOG, "w") as log:
        for step in config["build_steps"]:
            log.write(f"$ {step}\n")
            log.flush()
            code = subprocess.call(step, shell=True, cwd=config["repo_dir"], executable="/bin/bash",
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            if code != 0:
                log.write(f"\n!!! step failed with exit code {code}\n")
                ok = False
                break
        else:
            log.write("\nBuild finished OK.\n")

    build_state.update(state="success" if ok else "FAILED", finished=now_str())
    if not ok:
        set_message("Build failed, servers left as they were. See the build log.")
        return
    if not then_run:
        set_message("Build finished OK.")
        return
    with action_lock:
        results = [restart_server(server) for server in config["servers"]]
    set_message("Build finished OK. " + " ".join(results))


def start_build(then_run):
    if build_state["state"] == "running":
        return "A build is already running."
    threading.Thread(target=run_build, args=(then_run,), daemon=True).start()
    return "Build started." + (" Servers will be restarted if it succeeds." if then_run else "")


# ---------------------------------------------------------------- stats

def read_cpu_ticks():
    line = read_file("/proc/stat")
    if not line:
        return None
    values = [int(v) for v in line.splitlines()[0].split()[1:]]
    idle = values[3] + values[4]  # idle + iowait
    return sum(values), idle


def read_proc_ticks(pid):
    stat = read_file(f"/proc/{pid}/stat")
    if not stat:
        return None
    fields = stat.rsplit(")", 1)[1].split()
    return int(fields[11]) + int(fields[12])  # utime + stime


def cpu_sampler():
    """Every 2 s, works out system CPU % and the CPU % of each server process."""
    last_total = read_cpu_ticks()
    last_procs = {}
    while True:
        time.sleep(2)
        total = read_cpu_ticks()
        if not total or not last_total:
            continue
        total_delta = total[0] - last_total[0]
        idle_delta = total[1] - last_total[1]
        if total_delta > 0:
            cpu_stats["system"] = 100.0 * (total_delta - idle_delta) / total_delta

        # Process ticks are summed over all cores; divide by the per-core tick count.
        cores = os.cpu_count() or 1
        per_core_delta = total_delta / cores
        procs = {}
        percents = {}
        for server in config["servers"]:
            for pid in find_pids(server["exe"]):
                ticks = read_proc_ticks(pid)
                if ticks is None:
                    continue
                procs[pid] = ticks
                if pid in last_procs and per_core_delta > 0:
                    percents[pid] = 100.0 * (ticks - last_procs[pid]) / per_core_delta
        cpu_stats["procs"] = percents
        last_procs = procs
        last_total = total


def system_stats():
    stats = {"cpu": cpu_stats["system"], "cores": os.cpu_count()}
    meminfo = read_file("/proc/meminfo")
    if meminfo:
        values = {}
        for line in meminfo.splitlines():
            key, rest = line.split(":", 1)
            values[key] = int(rest.split()[0])
        stats["ram_total"] = values["MemTotal"] / 1024
        stats["ram_used"] = (values["MemTotal"] - values["MemAvailable"]) / 1024
    temp = read_file("/sys/class/thermal/thermal_zone0/temp")
    if temp:
        stats["temp"] = int(temp) / 1000
    uptime = read_file("/proc/uptime")
    if uptime:
        stats["uptime"] = float(uptime.split()[0])
    if hasattr(os, "getloadavg"):
        stats["load"] = os.getloadavg()
    disk = shutil.disk_usage(config["repo_dir"] if os.path.isdir(config["repo_dir"]) else HERE)
    stats["disk_free"] = disk.free / 1024 ** 3
    stats["disk_total"] = disk.total / 1024 ** 3
    return stats


# ---------------------------------------------------------------- page

PAGE_STYLE = """
body { font-family: sans-serif; max-width: 960px; margin: 16px auto; padding: 0 16px; }
section { border: 1px solid #ccc; border-radius: 4px; padding: 0 12px 8px; margin-bottom: 16px; }
table { border-collapse: collapse; margin-bottom: 8px; }
td, th { border: 1px solid #ddd; padding: 4px 10px; text-align: left; }
pre { background: #111; color: #ddd; padding: 8px; overflow-x: auto; font-size: 12px; white-space: pre-wrap; }
form { display: inline; }
button { padding: 4px 12px; margin-right: 4px; }
.running { color: green; font-weight: bold; }
.stopped { color: #b00; font-weight: bold; }
.message { background: #ffd; border: 1px solid #cc9; padding: 8px; }
.log-error { color: #f66; }
.log-warning { color: #fa4; }
.log-success { color: #6d6; }
.log-debug { color: #dd6; }
.log-marker { color: #8af; }
"""

LOG_LEVEL_CLASSES = [("[ERROR]", "log-error"), ("!!!", "log-error"), ("[WARNING]", "log-warning"),
                     ("[SUCCESS]", "log-success"), ("[DEBUG]", "log-debug"), ("=====", "log-marker")]


def button(action, label, server=None, confirm=None):
    hidden = f'<input type="hidden" name="server" value="{html.escape(server)}">' if server else ""
    onclick = f' onclick="return confirm(\'{confirm}\')"' if confirm else ""
    return (f'<form method="post" action="/action"><input type="hidden" name="action" value="{action}">'
            f'{hidden}<button{onclick}>{label}</button></form>')


def fmt(value, pattern, missing="-"):
    return pattern.format(value) if value is not None else missing


def render_log_lines(text):
    """Log text as HTML, each line coloured by its log level."""
    lines = []
    for line in text.splitlines():
        css = next((c for marker, c in LOG_LEVEL_CLASSES if marker in line), None)
        line = html.escape(line)
        lines.append(f"<span class='{css}'>{line}</span>" if css else line)
    return "<pre>" + "\n".join(lines) + "</pre>"


def page_head(title, refresh):
    meta = "<meta http-equiv='refresh' content='5'>" if refresh else ""
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
            f"<meta name='viewport' content='width=device-width, initial-scale=1'>{meta}"
            f"<style>{PAGE_STYLE}</style></head><body>")


def render_system():
    stats = system_stats()
    load = stats.get("load")
    out = ["<section><h2>System</h2><table>"]
    out.append(f"<tr><th>CPU</th><td>{fmt(stats['cpu'], '{:.0f}%')} ({stats['cores']} cores)</td></tr>")
    if "ram_total" in stats:
        out.append(f"<tr><th>RAM</th><td>{stats['ram_used']:.0f} / {stats['ram_total']:.0f} MB</td></tr>")
    out.append(f"<tr><th>Temperature</th><td>{fmt(stats.get('temp'), '{:.1f} &deg;C')}</td></tr>")
    out.append(f"<tr><th>Load</th><td>{' '.join(f'{v:.2f}' for v in load) if load else '-'}</td></tr>")
    out.append(f"<tr><th>Disk free</th><td>{stats['disk_free']:.1f} / {stats['disk_total']:.1f} GB</td></tr>")
    out.append(f"<tr><th>Uptime</th><td>{format_duration(stats['uptime']) if 'uptime' in stats else '-'}</td></tr>")
    out.append("</table></section>")
    return "".join(out)


def render_server(server):
    esc = html.escape
    name = server["name"]
    pids = find_pids(server["exe"])
    info = process_info(pids[0]) if pids else None
    cpu = cpu_stats["procs"].get(pids[0]) if pids else None

    out = [f"<section><h2>{esc(name)}</h2><p>"]
    if pids:
        out.append("<span class='running'>RUNNING</span> ")
        out.append(button("stop", "Stop", name, f"Stop {name}?"))
        out.append(button("restart", "Restart", name, f"Restart {name}?"))
    else:
        out.append("<span class='stopped'>STOPPED</span> ")
        out.append(button("start", "Start", name))
    out.append("</p><table><tr><th>PID</th><th>Up for</th><th>RAM</th><th>CPU (of one core)</th></tr>")
    out.append(f"<tr><td>{', '.join(map(str, pids)) or '-'}</td>"
               f"<td>{format_duration(info['uptime']) if info else '-'}</td>"
               f"<td>{fmt(info and info['ram_mb'], '{:.1f} MB')}</td><td>{fmt(cpu, '{:.0f}%')}</td></tr></table>")
    out.append(f"<p>Log (last 25 lines) &middot; <a href='/log?name={quote(name)}'>full log</a></p>")
    out.append(render_log_lines(tail(server_log_path(server), 25)))
    out.append("</section>")
    return "".join(out)


def render_build():
    out = ["<section><h2>Build</h2><p>"]
    out.append(f"State: <b>{html.escape(build_state['state'])}</b>")
    if build_state["started"]:
        out.append(f" &middot; started {build_state['started']}")
    if build_state["finished"]:
        out.append(f" &middot; finished {build_state['finished']}")
    out.append("</p><p>")
    out.append(button("build_run", "Build &amp; Run", confirm="Pull, build, then restart all servers?"))
    out.append(button("build", "Build only"))
    out.append(" <a href='/log?name=build'>full build log</a></p>")
    out.append(render_log_lines(tail(BUILD_LOG, 15)))
    out.append("</section>")
    return "".join(out)


def render_page():
    out = [page_head("Server Manager", refresh=True), "<h1>Server Manager</h1>"]
    if message["text"] and time.time() - message["time"] < 60:
        out.append(f"<p class='message'>{html.escape(message['text'])}</p>")
    out.append(render_system())
    for server in config["servers"]:
        out.append(render_server(server))
    out.append(render_build())
    out.append("</body></html>")
    return "".join(out)


def render_log(name):
    if name == "build":
        path = BUILD_LOG
    else:
        server = find_server(name)
        if not server:
            return None
        path = server_log_path(server)
    return (page_head(f"{name} log", refresh=False) + "<p><a href='/'>&larr; back</a> &middot; "
            f"<a href='/log?name={quote(name)}'>reload</a></p><h1>{html.escape(name)} log</h1>"
            + render_log_lines(tail(path, 1000)) + "</body></html>")


# ---------------------------------------------------------------- http

def handle_action(action, server_name):
    if action == "build":
        return start_build(then_run=False)
    if action == "build_run":
        return start_build(then_run=True)
    server = find_server(server_name)
    if not server:
        return f"Unknown server: {server_name}"
    handlers = {"start": start_server, "stop": stop_server, "restart": restart_server}
    if action not in handlers:
        return f"Unknown action: {action}"
    if not action_lock.acquire(blocking=False):
        return "Another action is in progress, try again in a moment."
    try:
        return handlers[action](server)
    finally:
        action_lock.release()


class Handler(BaseHTTPRequestHandler):
    def send_html(self, body, code=200):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self.send_html(render_page())
        elif url.path == "/log":
            page = render_log(parse_qs(url.query).get("name", [""])[0])
            self.send_html(page or "Not found", 200 if page else 404)
        else:
            self.send_html("Not found", 404)

    def do_POST(self):
        if self.path != "/action":
            self.send_html("Not found", 404)
            return
        length = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(length).decode())
        result = handle_action(form.get("action", [""])[0], form.get("server", [""])[0])
        set_message(result)
        print(f"[{now_str()}] {self.client_address[0]}: {result}", flush=True)
        self.send_response(303)  # Redirect back so a page refresh doesn't repeat the action.
        self.send_header("Location", "/")
        self.end_headers()

    def log_message(self, fmt, *args):
        pass  # Keep the console quiet; actions are printed in do_POST.


def main():
    global config
    config_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "config.json")
    config = load_config(config_path)
    os.makedirs(LOG_DIR, exist_ok=True)

    for server in config["servers"]:
        if server.get("autostart"):
            print(start_server(server), flush=True)

    threading.Thread(target=cpu_sampler, daemon=True).start()
    host, port = config.get("host", "0.0.0.0"), config.get("port", 8080)
    print(f"Server manager on http://{host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
