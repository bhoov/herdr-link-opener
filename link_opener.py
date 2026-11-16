#!/usr/bin/env python3
"""Open Ctrl-clicked links with other schemes than http(s) on the computer with the desktop.

Herdr opens only http:// and https:// links. For every other link it runs a plugin link
handler on the Herdr server that owns the pane. This plugin is that handler.

The plugin has two roles, chosen per machine:

- desktop: the computer where you run the Herdr TUI. `open` gives the link to the operating
  system (`open` on macOS, `xdg-open` on Linux). A background listener keeps one SSH connection
  to each enabled saved machine and opens the links that arrive from it.
- relay: a machine without a desktop, reached over SSH. `open` appends the link to a queue
  file. The listener on the desktop reads that file through its SSH connection.

Only links whose scheme is in "schemes" (config.json on the desktop) open.

Usage: link_opener.py start|run|stop|restart|status|open [URL]
"""

import fcntl
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional

PLUGIN_ID = "bhoov.link-opener"
HERDR = os.environ.get("HERDR_BIN_PATH") or "herdr"
# Outside Herdr (for example `link_opener.py status` in a shell), use the same directories that
# Herdr gives the plugin on Linux and macOS, so that both see one config file and one listener lock.
STATE_DIR = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "herdr", "plugins", PLUGIN_ID)
CONFIG_DIR = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "herdr", "plugins", "config", PLUGIN_ID)
LOCK_PATH = os.path.join(STATE_DIR, "daemon.lock")
PID_PATH = os.path.join(STATE_DIR, "daemon.pid")
LOG_PATH = os.path.join(STATE_DIR, "daemon.log")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# The queue is in /tmp, which is local to one node. A home directory can be shared by several
# nodes, and a queue there would send one click to the listener of every node.
QUEUE_DIR_TEMPLATE = "/tmp/herdr-link-opener-{uid}"
QUEUE_NAME = "queue"
MAX_URL_CHARS = 4096
# A listener opens at most this many links from one machine in RATE_WINDOW_SECONDS.
RATE_LIMIT = 5
RATE_WINDOW_SECONDS = 10
MACHINE_REFRESH_SECONDS = 30

DEFAULT_CONFIG = {
    "schemes": ["vscode", "vscode-insiders"],  # URL schemes that open; all other links are refused
    "role": "auto",  # "auto", "desktop" or "relay"
    "machines": None,  # labels or ids to listen to; null means every enabled saved machine
    "opener": None,  # argv that receives the URL as its last argument; null means open / xdg-open
    "notify": True,  # show a toast when a link is refused or cannot be delivered
}

_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")

# Runs on the relay machine through SSH. It prints one line per appended link.
# `tail -n 0` skips links that were clicked while no listener was connected.
# `cat` ends when the SSH connection closes its input; the script then stops `tail`, so that a
# running `tail` means a connected listener.
READER_SCRIPT = """
d=/tmp/herdr-link-opener-$(id -u)
mkdir -m 700 -p "$d" || exit 3
if [ -L "$d" ] || [ ! -d "$d" ] || [ ! -O "$d" ]; then echo "unsafe queue directory $d" >&2; exit 3; fi
chmod 700 "$d" || exit 3
: >> "$d/queue" || exit 3
echo ready
tail -n 0 -F "$d/queue" &
trap 'kill $!' EXIT
cat > /dev/null
"""


def log(message: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------------------------
# Pure helpers


def url_scheme(url: str) -> Optional[str]:
    """Return the lowercase scheme of a URL that is safe to pass on, else None."""
    if len(url) > MAX_URL_CHARS or any(ord(char) <= 0x20 or ord(char) == 0x7F for char in url):
        return None
    match = _SCHEME.match(url)
    return match.group(1).lower() if match else None


def scheme_allowed(url: str, schemes: List[str]) -> bool:
    scheme = url_scheme(url)
    return scheme is not None and scheme in {item.lower().rstrip(":/") for item in schemes}


def choose_role(configured: str, platform: str, environ: Dict[str, str]) -> str:
    if configured in ("desktop", "relay"):
        return configured
    if environ.get("SSH_CONNECTION") or environ.get("SSH_TTY"):
        return "relay"
    if platform == "darwin" or environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY"):
        return "desktop"
    return "relay"


class RateLimit:
    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._times: Deque[float] = deque()

    def allow(self, now: float) -> bool:
        while self._times and now - self._times[0] > self.window_seconds:
            self._times.popleft()
        if len(self._times) >= self.limit:
            return False
        self._times.append(now)
        return True


# ---------------------------------------------------------------------------------------------
# Config, notifications, opening


def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH) as file:
            loaded = json.load(file)
    except (OSError, ValueError):
        loaded = {}
    if isinstance(loaded, dict):
        config.update({key: value for key, value in loaded.items() if key in DEFAULT_CONFIG})
    # A wrong type falls back to the default. For example, a string "opener" would otherwise
    # become one argv entry per character.
    checks = {
        "schemes": lambda value: isinstance(value, list) and all(isinstance(item, str) for item in value),
        "role": lambda value: value in ("auto", "desktop", "relay"),
        "machines": lambda value: value is None or (isinstance(value, list) and all(isinstance(item, str) for item in value)),
        "opener": lambda value: value is None or (isinstance(value, list) and len(value) > 0 and all(isinstance(item, str) for item in value)),
        "notify": lambda value: isinstance(value, bool),
    }
    for key, valid in checks.items():
        if not valid(config[key]):
            log(f"config: invalid {key!r} in {CONFIG_PATH}, using {DEFAULT_CONFIG[key]!r}")
            config[key] = DEFAULT_CONFIG[key]
    return config


def role(config: dict) -> str:
    return choose_role(config["role"], sys.platform, dict(os.environ))


def notify(config: dict, title: str, body: str) -> None:
    if not config["notify"]:
        return
    try:
        subprocess.run([HERDR, "notification", "show", title, "--body", body], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        log(f"notification failed: {error}")


def open_on_desktop(config: dict, url: str, source: str) -> bool:
    """Give an allowed URL to the operating system. `source` names where the click came from."""
    if not scheme_allowed(url, config["schemes"]):
        scheme = url_scheme(url)
        log(f"{source}: refused {url[:200]!r}")
        notify(config, "Link not opened", f"Scheme {scheme}: is not in \"schemes\" in {CONFIG_PATH}" if scheme else "The link is not a valid URL.")
        return False
    opener = config["opener"] or (["open"] if sys.platform == "darwin" else ["xdg-open"])
    try:
        subprocess.Popen([*opener, url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as error:
        log(f"{source}: cannot open {url}: {error}")
        notify(config, "Link not opened", str(error))
        return False
    log(f"{source}: opened {url}")
    return True


# ---------------------------------------------------------------------------------------------
# Relay: append the link to the queue


def queue_dir() -> str:
    """Create the queue directory, or raise when another user could write to it."""
    path = QUEUE_DIR_TEMPLATE.format(uid=os.getuid())
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError(f"unsafe queue directory {path}")
    if info.st_mode & 0o077:
        os.chmod(path, 0o700)
    return path


def reader_runs(queue_path: str) -> bool:
    """Return whether a listener's `tail` reads the queue."""
    try:
        # Anchored, so that another process whose command line only mentions the queue does not count.
        result = subprocess.run(["pgrep", "-u", str(os.getuid()), "-f", f"^tail -n 0 -F {queue_path}$"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return True  # cannot tell; do not show a false warning
    return result.returncode == 0


def relay(config: dict, url: str) -> int:
    if url_scheme(url) is None:
        notify(config, "Link not opened", "The link is not a valid URL.")
        return 1
    queue_path = os.path.join(queue_dir(), QUEUE_NAME)
    descriptor = os.open(queue_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.write(descriptor, (url + "\n").encode())
    finally:
        os.close(descriptor)
    if not reader_runs(queue_path):
        notify(config, "Link not opened", "No computer listens for links from this machine. Install the Link Opener plugin on the computer where you run Herdr, and save this machine there.")
        return 1
    return 0


def open_command(argv: List[str]) -> int:
    url = argv[0] if argv else os.environ.get("HERDR_PLUGIN_CLICKED_URL", "")
    if not url:
        print("no URL: pass one, or run this as a Herdr link handler", file=sys.stderr)
        return 2
    config = load_config()
    if role(config) == "desktop":
        return 0 if open_on_desktop(config, url, "local") else 1
    return relay(config, url)


# ---------------------------------------------------------------------------------------------
# Desktop: one SSH connection per saved machine


@dataclass(frozen=True)
class Machine:
    id: str
    label: str
    target: str


class MachineWorker(threading.Thread):
    def __init__(self, machine: Machine, config: dict) -> None:
        super().__init__(name=f"machine-{machine.label}", daemon=True)
        self.machine = machine
        self.config = config
        self.stop_event = threading.Event()
        self.process: Optional[subprocess.Popen] = None
        self.rate = RateLimit(RATE_LIMIT, RATE_WINDOW_SECONDS)

    def command(self) -> List[str]:
        # ClearAllForwardings: the user's ssh config may declare forwards that an interactive session already holds.
        return [
            "ssh", "-T",
            "-o", "BatchMode=yes",
            "-o", "ClearAllForwardings=yes",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            self.machine.target,
            "sh -c " + shlex.quote(READER_SCRIPT),
        ]

    def run(self) -> None:
        delay = 5
        while not self.stop_event.is_set():
            started = time.monotonic()
            self.read_links()
            if self.stop_event.is_set():
                break
            # A connection that lasted a while was a working one; retry it soon.
            delay = 5 if time.monotonic() - started > 60 else min(delay * 2, 120)
            self.stop_event.wait(delay)

    def read_links(self) -> None:
        try:
            self.process = subprocess.Popen(self.command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
        except OSError as error:
            log(f"{self.machine.label}: cannot start ssh: {error}")
            return
        process = self.process
        if self.stop_event.is_set():
            process.stdin.close()  # stop() ran before self.process was set
        ready = False
        for line in process.stdout:
            line = line.rstrip("\n")
            if not ready:
                ready = line == "ready"
                if ready:
                    log(f"{self.machine.label}: listening")
                continue
            if self.stop_event.is_set():
                break
            if not self.rate.allow(time.monotonic()):
                log(f"{self.machine.label}: too many links, dropped {line[:200]!r}")
                continue
            open_on_desktop(self.config, line, self.machine.label)
        process.wait()
        stderr = process.stderr.read().strip()
        if not self.stop_event.is_set():
            log(f"{self.machine.label}: connection ended (exit {process.returncode}){': ' + stderr.splitlines()[-1] if stderr else ''}")

    def stop(self) -> None:
        self.stop_event.set()
        if self.process is not None and self.process.poll() is None:
            self.process.stdin.close()  # ends the remote script, which then stops its `tail`
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.terminate()


def enabled_machines(config: dict) -> List[Machine]:
    result = subprocess.run([HERDR, "machine", "list", "--json"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "herdr machine list failed")
    selected = config["machines"]
    machines = []
    for item in json.loads(result.stdout):
        if not item.get("enabled"):
            continue
        if selected is not None and item["id"] not in selected and item["label"] not in selected:
            continue
        machines.append(Machine(item["id"], item["label"], item["target"]))
    return machines


def acquire_lock():
    os.makedirs(STATE_DIR, exist_ok=True)
    handle = open(LOCK_PATH, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def daemon_runs() -> bool:
    handle = acquire_lock()
    if handle is None:
        return True
    handle.close()
    return False


def run_daemon() -> int:
    lock = acquire_lock()
    if lock is None:
        log("another listener already runs")
        return 0
    with open(PID_PATH, "w") as file:
        file.write(str(os.getpid()))
    config = load_config()
    stop = threading.Event()
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, lambda *_: stop.set())
    server_socket = os.environ.get("HERDR_SOCKET_PATH")
    workers: Dict[str, MachineWorker] = {}
    missing_server_checks = 0
    log(f"listener started, pid {os.getpid()}, schemes {config['schemes']}")
    while not stop.is_set():
        if server_socket and not os.path.exists(server_socket):
            missing_server_checks += 1
            if missing_server_checks >= 3:
                log("local herdr server is gone")
                break
        else:
            missing_server_checks = 0
        try:
            machines = {machine.id: machine for machine in enabled_machines(config)}
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
            log(f"cannot list machines: {error}")
            machines = {machine_id: worker.machine for machine_id, worker in workers.items()}
        for machine_id, worker in list(workers.items()):
            if machine_id not in machines or machines[machine_id] != worker.machine or not worker.is_alive():
                worker.stop()
                worker.join(timeout=10)
                del workers[machine_id]
        for machine_id, machine in machines.items():
            if machine_id not in workers:
                workers[machine_id] = MachineWorker(machine, config)
                workers[machine_id].start()
        stop.wait(MACHINE_REFRESH_SECONDS if missing_server_checks == 0 else 10)
    for worker in workers.values():
        worker.stop()
    for worker in workers.values():
        worker.join(timeout=10)
    try:
        os.unlink(PID_PATH)
    except FileNotFoundError:
        pass
    log("listener stopped")
    return 0


def start_daemon() -> int:
    if role(load_config()) != "desktop":
        print("this machine has no desktop; links go to the queue and no listener starts")
        return 0
    if daemon_runs():
        print("link listener already runs")
        return 0
    os.makedirs(STATE_DIR, exist_ok=True)
    if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1_000_000:
        os.replace(LOG_PATH, LOG_PATH + ".1")
    with open(LOG_PATH, "a") as log_file:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "run"], stdin=subprocess.DEVNULL, stdout=log_file, stderr=log_file, start_new_session=True, cwd=os.path.dirname(os.path.abspath(__file__)))
    print(f"link listener started; log: {LOG_PATH}")
    return 0


def stop_daemon() -> int:
    if not daemon_runs():
        print("link listener does not run")
        return 0
    try:
        with open(PID_PATH) as file:
            pid = int(file.read().strip())
    except (OSError, ValueError):
        print("link listener runs, but its pid file is missing")
        return 1
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and daemon_runs():
        time.sleep(0.2)
    print("link listener stopped" if not daemon_runs() else f"link listener (pid {pid}) did not stop")
    return 0


def status() -> int:
    config = load_config()
    print(f"role: {role(config)}")
    print(f"schemes: {' '.join(config['schemes'])}")
    print(f"config: {CONFIG_PATH}")
    if role(config) == "desktop":
        print(f"listener: {'runs' if daemon_runs() else 'does not run'}; log: {LOG_PATH}")
    else:
        queue_path = os.path.join(QUEUE_DIR_TEMPLATE.format(uid=os.getuid()), QUEUE_NAME)
        print(f"queue: {queue_path}; a computer {'listens' if os.path.exists(queue_path) and reader_runs(queue_path) else 'does not listen'}")
    return 0


def main(argv: List[str]) -> int:
    commands = {
        "start": start_daemon,
        "run": run_daemon,
        "stop": stop_daemon,
        "restart": lambda: stop_daemon() or start_daemon(),
        "status": status,
    }
    if len(argv) >= 2 and argv[1] == "open" and len(argv) <= 3:
        return open_command(argv[2:])
    if len(argv) != 2 or argv[1] not in commands:
        print(__doc__, file=sys.stderr)
        return 2
    return commands[argv[1]]()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
