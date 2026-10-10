#!/usr/bin/env python3
"""Small web dashboard to build, start, stop and monitor the game servers on the Pi.

Standard library only. Run: python3 server_manager.py [config.json]
"""

import collections
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
BUILD_INFO = os.path.join(LOG_DIR, "build_info.json")
MAX_LOG_BYTES = 5 * 1024 * 1024
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

SAMPLE_SECONDS = 2  # How often CPU, network and the crash watchdog run.
HISTORY_SECONDS = 10  # How often a point is added to the history charts.
HISTORY_KEEP = 6 * 3600  # How much history is kept in memory.
HISTORY_RANGES = {"15m": 900, "1h": 3600, "6h": 6 * 3600}
CHART_POINTS = 150  # Max points drawn per chart; longer ranges are averaged down.
CRASH_LIMIT = 3  # Auto-restart gives up after this many crashes...
CRASH_WINDOW = 600  # ...within this many seconds.

THROTTLE_FLAGS = [(0, "under-voltage"), (1, "frequency capped"), (2, "throttled"), (3, "soft temperature limit")]

config = {}
children = []  # Popen objects we started, kept so exited ones get reaped.
action_lock = threading.Lock()  # One start/stop/build action at a time.
message = {"text": "", "time": 0.0}
build_state = {"state": "not run yet"}
servers_state = {}  # Per server name: wanted, proc, commit, last_crash, crash_times, gave_up.
live = {"cpu": None, "procs": {}, "net": None, "throttled": None}  # Filled by the sampler thread.
history = collections.deque(maxlen=HISTORY_KEEP // HISTORY_SECONDS)  # (time, cpu %, ram MB, temp, rx B/s, tx B/s)


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


def time_str(epoch):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


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


def format_rate(bytes_per_second):
    if bytes_per_second >= 1024 * 1024:
        return f"{bytes_per_second / (1024 * 1024):.1f} MB/s"
    if bytes_per_second >= 1024:
        return f"{bytes_per_second / 1024:.1f} KB/s"
    return f"{bytes_per_second:.0f} B/s"


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


def append_log(server, text):
    with open(server_log_path(server), "a") as log:
        log.write(f"\n===== {text} =====\n")


def server_log_path(server):
    return os.path.join(LOG_DIR, server["name"] + ".log")


def find_server(name):
    for server in config["servers"]:
        if server["name"] == name:
            return server
    return None


def git_commit():
    """Short hash and subject of the repo's current commit, or (None, None)."""
    try:
        out = subprocess.run(["git", "-C", config["repo_dir"], "log", "-1", "--format=%h%x00%s"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None, None
    if "\0" not in out:
        return None, None
    commit, subject = out.split("\0", 1)
    return commit, subject


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
    """RAM (MB), uptime (seconds) and start time (epoch) of a process, or None if it is gone."""
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
    return {"ram_mb": rss_kb / 1024, "uptime": age, "started": time.time() - age}


def describe_exit(code):
    if code is None:
        return "exit code unknown (not started by the dashboard)"
    if code < 0:
        try:
            name = signal.Signals(-code).name
        except ValueError:
            name = f"signal {-code}"
        return f"killed by {name}"
    return f"exit code {code}"


def start_server(server):
    state = servers_state[server["name"]]
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
        return f"{server['name']} exited right away ({describe_exit(proc.returncode)}). Check its log."
    # The binary is from the last build only if that build succeeded after the binary was written.
    commit = build_state.get("commit") if build_state.get("state") == "success" else None
    state.update(wanted=True, proc=proc, commit=commit)
    return f"{server['name']} started (pid {proc.pid})."


def stop_server(server):
    state = servers_state[server["name"]]
    state.update(wanted=False, gave_up=False)  # Set first so the watchdog doesn't call this a crash.
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


def watchdog():
    """Spots servers that died without being stopped from the dashboard, and auto-restarts them if enabled."""
    if not action_lock.acquire(blocking=False):
        return  # A start/stop/build is in progress; check again next time.
    try:
        for server in config["servers"]:
            state = servers_state[server["name"]]
            if not state["wanted"] or find_pids(server["exe"]):
                continue
            proc = state["proc"]
            code = proc.poll() if proc else None
            now = time.time()
            state["wanted"] = False
            state["last_crash"] = {"time": now, "reason": describe_exit(code)}
            state["crash_times"] = [t for t in state["crash_times"] if now - t < CRASH_WINDOW] + [now]
            append_log(server, f"stopped unexpectedly {now_str()}: {describe_exit(code)}")
            print(f"[{now_str()}] {server['name']} stopped unexpectedly: {describe_exit(code)}", flush=True)

            if not server.get("auto_restart"):
                continue
            if len(state["crash_times"]) >= CRASH_LIMIT:
                state["gave_up"] = True
                append_log(server, f"auto-restart gave up: {CRASH_LIMIT} crashes in {CRASH_WINDOW // 60} min")
                continue
            append_log(server, "auto-restarting")
            print(f"[{now_str()}] {start_server(server)}", flush=True)
    finally:
        action_lock.release()


# ---------------------------------------------------------------- build

def save_build_info():
    with open(BUILD_INFO, "w") as f:
        json.dump(build_state, f)


def load_build_info():
    try:
        with open(BUILD_INFO) as f:
            build_state.update(json.load(f))
    except (OSError, ValueError):
        return
    if build_state.get("state") == "running":
        build_state["state"] = "interrupted"  # The dashboard was stopped mid-build.


def run_build(then_run, clean):
    """Runs the configured build steps in order (clean steps first if asked), stopping at the first failure."""
    started = time.time()
    build_state.clear()
    build_state.update(state="running", started=started, clean=clean)
    steps = (config.get("clean_steps", []) if clean else []) + config["build_steps"]
    ok = True
    with open(BUILD_LOG, "w") as log:
        for step in steps:
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

    commit, subject = git_commit()
    build_state.update(state="success" if ok else "FAILED", finished=time.time(),
                       duration=time.time() - started, commit=commit, subject=subject)
    save_build_info()
    if not ok:
        set_message("Build failed, servers left as they were. See the build log.")
        return
    if not then_run:
        set_message("Build finished OK.")
        return
    with action_lock:
        results = [restart_server(server) for server in config["servers"]]
    set_message("Build finished OK. " + " ".join(results))


def start_build(then_run, clean=False):
    if build_state.get("state") == "running":
        return "A build is already running."
    build_state["state"] = "running"  # Set now so a quick second click can't start another build.
    threading.Thread(target=run_build, args=(then_run, clean), daemon=True).start()
    return ("Clean build" if clean else "Build") + " started." + (" Servers will be restarted if it succeeds." if then_run else "")


# ---------------------------------------------------------------- stats

def read_cpu_ticks():
    text = read_file("/proc/stat")
    if not text:
        return None
    values = [int(v) for v in text.splitlines()[0].split()[1:]]
    idle = values[3] + values[4]  # idle + iowait
    return sum(values), idle


def read_proc_ticks(pid):
    stat = read_file(f"/proc/{pid}/stat")
    if not stat:
        return None
    fields = stat.rsplit(")", 1)[1].split()
    return int(fields[11]) + int(fields[12])  # utime + stime


def read_net_bytes():
    """Total (rx bytes, rx packets, tx bytes, tx packets) over all interfaces except loopback."""
    text = read_file("/proc/net/dev")
    if not text:
        return None
    totals = [0, 0, 0, 0]
    for line in text.splitlines()[2:]:
        name, data = line.split(":", 1)
        if name.strip() == "lo":
            continue
        fields = data.split()
        for i, field in enumerate((0, 1, 8, 9)):
            totals[i] += int(fields[field])
    return totals


def read_memory():
    """(used MB, total MB), or None."""
    text = read_file("/proc/meminfo")
    if not text:
        return None
    values = {}
    for line in text.splitlines():
        key, rest = line.split(":", 1)
        values[key] = int(rest.split()[0])
    return (values["MemTotal"] - values["MemAvailable"]) / 1024, values["MemTotal"] / 1024


def read_temperature():
    text = read_file("/sys/class/thermal/thermal_zone0/temp")
    return int(text) / 1000 if text else None


def read_throttled():
    """Raspberry Pi throttle bit field, or None when not on a Pi."""
    text = read_file("/sys/devices/platform/soc/soc:firmware/get_throttled")
    if text is None and shutil.which("vcgencmd"):
        try:
            text = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True,
                                  timeout=5).stdout  # "throttled=0x50005"
        except (OSError, subprocess.SubprocessError):
            text = None
    try:
        return int(text.split("=")[-1].strip(), 16)
    except (AttributeError, ValueError):
        return None


def sampler():
    """Every few seconds: CPU and network rates, the crash watchdog, and a history point now and then."""
    last_total = read_cpu_ticks()
    last_net = read_net_bytes()
    last_time = time.time()
    last_procs = {}
    last_history = 0.0
    last_throttle = 0.0
    while True:
        time.sleep(SAMPLE_SECONDS)
        now = time.time()
        elapsed = now - last_time
        last_time = now

        total = read_cpu_ticks()
        if total and last_total:
            total_delta = total[0] - last_total[0]
            idle_delta = total[1] - last_total[1]
            if total_delta > 0:
                live["cpu"] = 100.0 * (total_delta - idle_delta) / total_delta

            # Process ticks are summed over all cores; divide by the per-core tick count.
            per_core_delta = total_delta / (os.cpu_count() or 1)
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
            live["procs"] = percents
            last_procs = procs
        last_total = total

        net = read_net_bytes()
        if net and last_net and elapsed > 0:
            live["net"] = [max(0, (a - b) / elapsed) for a, b in zip(net, last_net)]
        last_net = net

        if now - last_throttle >= 10:
            live["throttled"] = read_throttled()
            last_throttle = now

        if now - last_history >= HISTORY_SECONDS:
            memory = read_memory()
            rates = live["net"] or [None] * 4
            history.append((now, live["cpu"], memory[0] if memory else None, read_temperature(), rates[0], rates[2]))
            last_history = now

        watchdog()


def system_stats():
    stats = {"cpu": live["cpu"], "cores": os.cpu_count(), "temp": read_temperature(), "memory": read_memory()}
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
body { font-family: sans-serif; max-width: 960px; margin: 16px auto; padding: 0 16px; background: #fff; }
section { border: 1px solid #ccc; border-radius: 4px; padding: 0 12px 8px; margin-bottom: 16px; }
table { border-collapse: collapse; margin-bottom: 8px; }
td, th { border: 1px solid #ddd; padding: 4px 10px; text-align: left; }
pre { background: #111; color: #ddd; padding: 8px; overflow-x: auto; font-size: 12px; white-space: pre-wrap; }
form { display: inline; }
button { padding: 4px 12px; margin-right: 4px; }
.running { color: green; font-weight: bold; }
.stopped { color: #b00; font-weight: bold; }
.ok { color: green; }
.bad { color: #b00; font-weight: bold; }
.warn { color: #b60; }
.note { background: #fff4e0; border: 1px solid #e8c48a; padding: 6px 8px; }
.alert { background: #fde8e8; border: 1px solid #e0a0a0; padding: 6px 8px; }
.muted { color: #666; }
.message { background: #ffd; border: 1px solid #cc9; padding: 8px; }
.log-error { color: #f66; }
.log-warning { color: #fa4; }
.log-success { color: #6d6; }
.log-debug { color: #dd6; }
.log-marker { color: #8af; }
body.horizontal { max-width: none; }
.horizontal .columns { display: grid; gap: 16px; align-items: start; }
@media (max-width: 900px) { .horizontal .columns { grid-template-columns: 1fr !important; } }
.header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; }
.charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; margin-bottom: 8px; }
.chart h4 { margin: 4px 0; font-size: 14px; }
.chart h4 span { font-weight: normal; color: #444; }
.chart svg { width: 100%; height: auto; display: block; }
.legend { font-size: 12px; color: #444; }
.swatch { display: inline-block; width: 10px; height: 3px; vertical-align: middle; margin: 0 4px 0 8px; }
"""

SERIES_1 = "#2a78d6"  # Blue, categorical slot 1.
SERIES_2 = "#eb6834"  # Orange, categorical slot 2.

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


def page_head(title, refresh, body_class=""):
    meta = "<meta http-equiv='refresh' content='5'>" if refresh else ""
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
            f"<meta name='viewport' content='width=device-width, initial-scale=1'>{meta}"
            f"<style>{PAGE_STYLE}</style></head><body class='{body_class}'>")


def render_throttle():
    value = live["throttled"]
    if value is None:
        return "<span class='muted'>not available</span>"
    now_flags = [label for bit, label in THROTTLE_FLAGS if value & (1 << bit)]
    past_flags = [label for bit, label in THROTTLE_FLAGS if value & (1 << (bit + 16))]
    if not now_flags and not past_flags:
        return "<span class='ok'>OK</span>"
    parts = []
    if now_flags:
        parts.append(f"<span class='bad'>NOW: {', '.join(now_flags)}</span>")
    if past_flags:
        parts.append(f"<span class='warn'>since boot: {', '.join(past_flags)}</span>")
    return " &middot; ".join(parts) + " <span class='muted'>(under-voltage usually means a weak power supply)</span>"


def render_network():
    rates = live["net"]
    if rates is None:
        return "-"
    return (f"in {format_rate(rates[0])} ({rates[1]:.0f} pkt/s) &middot; "
            f"out {format_rate(rates[2])} ({rates[3]:.0f} pkt/s)")


def history_points(range_seconds):
    """History within the range, averaged down to at most CHART_POINTS points."""
    cutoff = time.time() - range_seconds
    points = [p for p in history if p[0] >= cutoff]
    step = max(1, -(-len(points) // CHART_POINTS))
    result = []
    for i in range(0, len(points), step):
        chunk = points[i:i + step]
        row = []
        for column in range(len(chunk[0])):
            values = [p[column] for p in chunk if p[column] is not None]
            row.append(sum(values) / len(values) if values else None)
        result.append(row)
    return result


def render_chart(title, current, points, series, y_min, y_max, label_fmt):
    """Small SVG line chart. `series` is a list of (name, colour, column index into points)."""
    width, height = 320, 120
    left, right, top, bottom = 60, 8, 8, 18
    plot_w, plot_h = width - left - right, height - top - bottom
    out = [f"<div class='chart'><h4>{html.escape(title)} <span>{current}</span></h4>"]
    if len(series) > 1:
        out.append("<div class='legend'>" + "".join(
            f"<span class='swatch' style='background:{colour}'></span>{html.escape(name)}"
            for name, colour, _ in series) + "</div>")
    if len(points) < 2:
        out.append("<p class='muted'>Collecting data&hellip;</p></div>")
        return "".join(out)

    t0, t1 = points[0][0], points[-1][0]
    span = max(t1 - t0, 1)

    def x(t):
        return left + (t - t0) / span * plot_w

    def y(v):
        v = min(max(v, y_min), y_max)
        return top + plot_h - (v - y_min) / (y_max - y_min) * plot_h

    svg = [f"<svg viewBox='0 0 {width} {height}' role='img' aria-label='{html.escape(title)} history'>"]
    for fraction in (0, 0.5, 1):  # Recessive grid with labels at bottom, middle, top.
        value = y_min + (y_max - y_min) * fraction
        gy = y(value)
        svg.append(f"<line x1='{left}' x2='{width - right}' y1='{gy:.1f}' y2='{gy:.1f}' stroke='#e4e4e4' stroke-width='1'/>")
        svg.append(f"<text x='{left - 4}' y='{gy + 3:.1f}' font-size='10' fill='#666' text-anchor='end'>"
                   f"{html.escape(label_fmt(value))}</text>")
    svg.append(f"<text x='{left}' y='{height - 4}' font-size='10' fill='#666'>{format_duration(span)} ago</text>")
    svg.append(f"<text x='{width - right}' y='{height - 4}' font-size='10' fill='#666' text-anchor='end'>now</text>")

    for _, colour, column in series:
        coords = " ".join(f"{x(p[0]):.1f},{y(p[column]):.1f}" for p in points if p[column] is not None)
        svg.append(f"<polyline points='{coords}' fill='none' stroke='{colour}' stroke-width='2' "
                   f"stroke-linejoin='round' stroke-linecap='round'/>")

    # Invisible hover strips: the browser shows each one's <title> as a tooltip.
    strip = plot_w / len(points)
    for p in points:
        values = " / ".join(f"{name}: {label_fmt(p[column])}" for name, _, column in series if p[column] is not None)
        svg.append(f"<rect x='{x(p[0]) - strip / 2:.1f}' y='{top}' width='{strip:.1f}' height='{plot_h}' "
                   f"fill='transparent'><title>{time.strftime('%H:%M:%S', time.localtime(p[0]))} "
                   f"&middot; {html.escape(values)}</title></rect>")
    svg.append("</svg>")
    out.extend(svg)
    out.append("</div>")
    return "".join(out)


def render_history(range_key):
    points = history_points(HISTORY_RANGES[range_key])
    memory = read_memory()
    ram_total = memory[1] if memory else 1024
    temps = [p[3] for p in points if p[3] is not None]
    net_max = max([p[c] for p in points for c in (4, 5) if p[c] is not None] + [10 * 1024])
    latest = points[-1] if points else [None] * 6

    links = " ".join(f"<b>{key}</b>" if key == range_key else f"<a href='/prefs?range={key}'>{key}</a>"
                     for key in HISTORY_RANGES)
    out = [f"<h3>History <small class='muted'>(kept in memory, cleared when the dashboard restarts) &middot; {links}</small></h3>",
           "<div class='charts'>"]
    out.append(render_chart("CPU", fmt(latest[1], "{:.0f}%"), points, [("CPU", SERIES_1, 1)], 0, 100,
                            lambda v: f"{v:.0f}%"))
    out.append(render_chart("RAM", fmt(latest[2], "{:.0f} MB"), points, [("RAM", SERIES_1, 2)], 0, ram_total,
                            lambda v: f"{v:.0f} MB"))
    out.append(render_chart("Temperature", fmt(latest[3], "{:.1f} &deg;C"), points, [("Temp", SERIES_1, 3)],
                            min(temps + [30]), max(temps + [90]), lambda v: f"{v:.0f}°C"))
    out.append(render_chart("Network", "", points, [("in", SERIES_1, 4), ("out", SERIES_2, 5)], 0, net_max * 1.1,
                            format_rate))
    out.append("</div>")
    return "".join(out)


def render_system(range_key):
    stats = system_stats()
    load = stats.get("load")
    memory = stats["memory"]
    out = ["<section><h2>System</h2><table>"]
    out.append(f"<tr><th>CPU</th><td>{fmt(stats['cpu'], '{:.0f}%')} ({stats['cores']} cores)</td></tr>")
    out.append(f"<tr><th>RAM</th><td>{f'{memory[0]:.0f} / {memory[1]:.0f} MB' if memory else '-'}</td></tr>")
    out.append(f"<tr><th>Temperature</th><td>{fmt(stats['temp'], '{:.1f} &deg;C')}</td></tr>")
    out.append(f"<tr><th>Power / throttling</th><td>{render_throttle()}</td></tr>")
    out.append(f"<tr><th>Network</th><td>{render_network()}</td></tr>")
    out.append(f"<tr><th>Load</th><td>{' '.join(f'{v:.2f}' for v in load) if load else '-'}</td></tr>")
    out.append(f"<tr><th>Disk free</th><td>{stats['disk_free']:.1f} / {stats['disk_total']:.1f} GB</td></tr>")
    out.append(f"<tr><th>Uptime</th><td>{format_duration(stats['uptime']) if 'uptime' in stats else '-'}</td></tr>")
    out.append("</table>")
    out.append(render_history(range_key))
    out.append("</section>")
    return "".join(out)


def render_server(server):
    esc = html.escape
    name = server["name"]
    state = servers_state[name]
    pids = find_pids(server["exe"])
    info = process_info(pids[0]) if pids else None
    cpu = live["procs"].get(pids[0]) if pids else None

    out = [f"<section><h2>{esc(name)}</h2><p>"]
    if pids:
        out.append("<span class='running'>RUNNING</span> ")
        out.append(button("stop", "Stop", name, f"Stop {name}?"))
        out.append(button("restart", "Restart", name, f"Restart {name}?"))
    else:
        out.append("<span class='stopped'>STOPPED</span> ")
        out.append(button("start", "Start", name))
    out.append(f" <span class='muted'>auto-restart {'on' if server.get('auto_restart') else 'off'}</span></p>")

    crash = state["last_crash"]
    if crash:
        css = "alert" if not pids else "note"
        text = f"Last crash: {time_str(crash['time'])}, {crash['reason']}."
        if state["gave_up"]:
            text += f" Auto-restart gave up after {CRASH_LIMIT} crashes in {CRASH_WINDOW // 60} minutes."
        elif pids and server.get("auto_restart"):
            text += " It was restarted automatically."
        out.append(f"<p class='{css}'>{esc(text)}</p>")

    # Code version: when the binary was built, which commit is running, and whether a restart is pending.
    if os.path.isfile(server["exe"]):
        built = os.path.getmtime(server["exe"])
        out.append(f"<p>Binary built: {time_str(built)} ({format_duration(time.time() - built)} ago)")
        if pids and state["commit"]:
            out.append(f"<br>Running commit: <code>{esc(state['commit'])}</code>")
        out.append("</p>")
        if info and built > info["started"] + 1:
            out.append("<p class='note'>Rebuilt since this server started; restart it to run the new code.</p>")
    else:
        out.append("<p class='muted'>Not built yet.</p>")

    out.append("<table><tr><th>PID</th><th>Up for</th><th>RAM</th><th>CPU (of one core)</th></tr>")
    out.append(f"<tr><td>{', '.join(map(str, pids)) or '-'}</td>"
               f"<td>{format_duration(info['uptime']) if info else '-'}</td>"
               f"<td>{fmt(info and info['ram_mb'], '{:.1f} MB')}</td><td>{fmt(cpu, '{:.0f}%')}</td></tr></table>")
    out.append(f"<p>Log (last 25 lines) &middot; <a href='/log?name={quote(name)}'>full log</a></p>")
    out.append(render_log_lines(tail(server_log_path(server), 25)))
    out.append("</section>")
    return "".join(out)


def render_build():
    esc = html.escape
    out = ["<section><h2>Build</h2><p>"]
    state = build_state.get("state", "not run yet")
    out.append(f"State: <b>{esc(state)}</b>{' (clean)' if build_state.get('clean') else ''}")
    if build_state.get("started"):
        out.append(f"<br>Started: {time_str(build_state['started'])}")
    if state == "running" and build_state.get("started"):
        out.append(f" &middot; running for {format_duration(time.time() - build_state['started'])}")
    if build_state.get("finished"):
        out.append(f"<br>Finished: {time_str(build_state['finished'])} &middot; took {format_duration(build_state['duration'])}")
    if build_state.get("commit"):
        out.append(f"<br>Commit: <code>{esc(build_state['commit'])}</code> {esc(build_state.get('subject') or '')}")
    out.append("</p><p>")
    out.append(button("build_run", "Build &amp; Run", confirm="Pull, build, then restart all servers?"))
    out.append(button("build", "Build only"))
    out.append(button("clean_build_run", "Clean Build &amp; Run",
                      confirm="Clean everything, rebuild from scratch (slow), then restart all servers?"))
    out.append(" <a href='/log?name=build'>full build log</a></p>")
    out.append(render_log_lines(tail(BUILD_LOG, 15)))
    out.append("</section>")
    return "".join(out)


def render_page(prefs):
    horizontal = prefs.get("layout") == "horizontal"
    range_key = prefs.get("range") if prefs.get("range") in HISTORY_RANGES else "1h"
    other = "vertical" if horizontal else "horizontal"
    toggle = f"<form method='get' action='/prefs'><input type='hidden' name='layout' value='{other}'>" \
             f"<button>Switch to {other} layout</button></form>"
    out = [page_head("Server Manager", refresh=True, body_class="horizontal" if horizontal else "")]
    out.append(f"<div class='header'><h1>Server Manager</h1>{toggle}</div>")
    if message["text"] and time.time() - message["time"] < 60:
        out.append(f"<p class='message'>{html.escape(message['text'])}</p>")
    out.append(render_system(range_key))
    # One column per server plus one for the build, side by side in the horizontal layout.
    columns = len(config["servers"]) + 1
    out.append(f"<div class='columns' style='grid-template-columns: repeat({columns}, minmax(0, 1fr))'>")
    for server in config["servers"]:
        out.append(render_server(server))
    out.append(render_build())
    out.append("</div></body></html>")
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

PREF_VALUES = {"layout": ("vertical", "horizontal"), "range": tuple(HISTORY_RANGES)}


def handle_action(action, server_name):
    if action == "build":
        return start_build(then_run=False)
    if action == "build_run":
        return start_build(then_run=True)
    if action == "clean_build_run":
        return start_build(then_run=True, clean=True)
    server = find_server(server_name)
    if not server:
        return f"Unknown server: {server_name}"
    handlers = {"start": start_server, "stop": stop_server, "restart": restart_server}
    if action not in handlers:
        return f"Unknown action: {action}"
    if not action_lock.acquire(blocking=False):
        return "Another action is in progress, try again in a moment."
    try:
        if action == "start":
            servers_state[server["name"]]["gave_up"] = False
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

    def redirect_home(self, cookies=()):
        self.send_response(303)
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Location", "/")
        self.end_headers()

    def prefs(self):
        """Page preferences (layout, chart range) stored in cookies."""
        prefs = {}
        for part in self.headers.get("Cookie", "").split(";"):
            key, _, value = part.strip().partition("=")
            if value in PREF_VALUES.get(key, ()):
                prefs[key] = value
        return prefs

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path == "/":
            self.send_html(render_page(self.prefs()))
        elif url.path == "/prefs":
            self.redirect_home([f"{key}={query[key][0]}; Path=/; Max-Age=31536000"
                                for key, allowed in PREF_VALUES.items()
                                if key in query and query[key][0] in allowed])
        elif url.path == "/log":
            page = render_log(query.get("name", [""])[0])
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
        self.redirect_home()  # Redirect back so a page refresh doesn't repeat the action.

    def log_message(self, fmt, *args):
        pass  # Keep the console quiet; actions are printed in do_POST.


def main():
    global config
    config_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "config.json")
    config = load_config(config_path)
    os.makedirs(LOG_DIR, exist_ok=True)
    load_build_info()

    for server in config["servers"]:
        # Servers already running (from before a dashboard restart, or started by hand) get watched too.
        servers_state[server["name"]] = {"wanted": bool(find_pids(server["exe"])), "proc": None, "commit": None,
                                         "last_crash": None, "crash_times": [], "gave_up": False}
        if server.get("autostart"):
            print(start_server(server), flush=True)

    threading.Thread(target=sampler, daemon=True).start()
    host, port = config.get("host", "0.0.0.0"), config.get("port", 8080)
    print(f"Server manager on http://{host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
