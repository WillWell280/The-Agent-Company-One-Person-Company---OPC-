#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local deployment manager for the AI Agent Collaboration Workspace.

Supports dependency installation, port selection, background startup, health
checks, stop/restart operations, logs, and browser launch.
"""

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from shutil import which


ROOT = Path(__file__).resolve().parent
APP_FILE = ROOT / "app" / "main.py"
APP_IMPORT = "app.main:app"
REQUIREMENTS_FILE = ROOT / "requirements.txt"
RUNTIME_DIR = ROOT / ".generic_agent_runtime" / "deploy"
PID_FILE = RUNTIME_DIR / "generic_agent.pid"
PORT_FILE = RUNTIME_DIR / "generic_agent.port"
URL_FILE = RUNTIME_DIR / "generic_agent.url"
LOG_FILE = RUNTIME_DIR / "generic_agent.log"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
PORT_SCAN_LIMIT = 80


def ensure_runtime_dir():
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def read_text(path):
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def write_text(path, value):
    ensure_runtime_dir()
    path.write_text(str(value), encoding="utf-8")


def remove_file(path):
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def read_pid():
    raw = read_text(PID_FILE)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def process_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_command(pid):
    if not pid or not process_alive(pid):
        return ""
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:
        return ""
    return proc.stdout.strip()


def cleanup_runtime_files():
    remove_file(PID_FILE)
    remove_file(PORT_FILE)
    remove_file(URL_FILE)


def current_status():
    pid = read_pid()
    port = read_text(PORT_FILE)
    url = read_text(URL_FILE)
    alive = process_alive(pid)
    if pid and not alive:
        cleanup_runtime_files()
        return {"running": False, "pid": None, "port": "", "url": "", "stale": True}
    return {"running": alive, "pid": pid, "port": port, "url": url, "stale": False}


def port_available(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def choose_port(host, preferred_port):
    for port in range(preferred_port, preferred_port + PORT_SCAN_LIMIT):
        if port_available(host, port):
            return port
    raise RuntimeError(
        f"No available port found in range {preferred_port}-{preferred_port + PORT_SCAN_LIMIT - 1}."
    )


def http_get(url, timeout=2.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read(200).decode("utf-8", errors="replace").strip()
            return resp.status, body
    except (urllib.error.URLError, TimeoutError, OSError):
        return None, ""


def wait_until_healthy(url, timeout=30.0):
    health_url = url.rstrip("/") + "/health"
    deadline = time.time() + timeout
    last_status = None
    last_body = ""
    while time.time() < deadline:
        last_status, last_body = http_get(health_url, timeout=2.0)
        if last_status == 200:
            return True, last_status, last_body
        last_status, last_body = http_get(url, timeout=2.0)
        if last_status == 200:
            return True, last_status, last_body
        time.sleep(0.5)
    return False, last_status, last_body


def uvicorn_available():
    proc = subprocess.run(
        [sys.executable, "-m", "uvicorn", "--version"],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    return proc.returncode == 0


def command_uses_caffeinate(command):
    return bool(command) and Path(command[0]).name == "caffeinate"


def build_start_command(port, host, disable_caffeinate=False):
    python_cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        APP_IMPORT,
        "--host",
        host,
        "--port",
        str(port),
    ]

    if sys.platform == "darwin" and not disable_caffeinate:
        caffeinate = which("caffeinate")
        if caffeinate:
            return [caffeinate, "-dimsu"] + python_cmd
    return python_cmd


def setup(_args):
    if not REQUIREMENTS_FILE.exists():
        raise SystemExit(f"Requirements file not found: {REQUIREMENTS_FILE}")
    print("Installing dependencies: python -m pip install -r requirements.txt")
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS_FILE)],
        cwd=str(ROOT),
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)
    print("Dependencies installed.")


def start(args):
    if not APP_FILE.exists():
        raise SystemExit(f"Application entry point not found: {APP_FILE}")

    status = current_status()
    if status["running"]:
        print(f"Service is already running: {status['url']} (pid={status['pid']})")
        if args.open:
            webbrowser.open(status["url"])
        return

    if not uvicorn_available():
        raise SystemExit(
            "Uvicorn is not installed in the current Python environment. Run: python3 deploy.py setup"
        )

    ensure_runtime_dir()
    port = args.port if args.port else choose_port(args.host, args.base_port)
    if args.port and not port_available(args.host, port):
        raise SystemExit(f"The requested port is already in use: {args.host}:{port}")

    url = f"http://localhost:{port}"
    command = build_start_command(port, args.host, args.no_caffeinate)

    with LOG_FILE.open("ab") as log:
        log.write(("\n\n===== start %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S")).encode("utf-8"))
        log.write(("command: %s\n" % " ".join(command)).encode("utf-8"))
        proc = subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            preexec_fn=os.setsid if hasattr(os, "setsid") else None,
        )

    write_text(PID_FILE, proc.pid)
    write_text(PORT_FILE, port)
    write_text(URL_FILE, url)

    healthy, status_code, body = wait_until_healthy(url, timeout=args.timeout)
    caffeinate_state = "on" if command_uses_caffeinate(command) else "off"
    if healthy:
        print(f"Service started: {url}")
        print(f"pid={proc.pid} port={port} caffeinate={caffeinate_state} health=ok")
        print(f"Log file: {LOG_FILE}")
        if args.open:
            webbrowser.open(url)
        return

    print(f"The service did not pass its health check within {args.timeout:.0f} seconds.")
    print(f"pid={proc.pid} port={port} caffeinate={caffeinate_state} last_status={status_code} body={body!r}")
    print("Review the logs: python3 deploy.py logs")
    raise SystemExit(1)


def stop(args):
    status = current_status()
    pid = status["pid"]
    if not status["running"]:
        print("Service is not running.")
        return

    print(f"Stopping service: pid={pid}")
    try:
        if hasattr(os, "killpg"):
            os.killpg(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        cleanup_runtime_files()
        print("Service stopped.")
        return

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        if not process_alive(pid):
            cleanup_runtime_files()
            print("Service stopped.")
            return
        time.sleep(0.2)

    print("Graceful shutdown timed out. Forcing the service to stop.")
    try:
        if hasattr(os, "killpg"):
            os.killpg(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    cleanup_runtime_files()
    print("Service force-stopped.")


def restart(args):
    stop_args = argparse.Namespace(timeout=args.stop_timeout)
    stop(stop_args)
    start_args = argparse.Namespace(
        host=args.host,
        base_port=args.base_port,
        port=args.port,
        open=args.open,
        timeout=args.timeout,
        no_caffeinate=args.no_caffeinate,
    )
    start(start_args)


def status(_args):
    state = current_status()
    if not state["running"]:
        suffix = " Stale runtime files were removed." if state.get("stale") else ""
        print(f"Service is not running.{suffix}")
        return

    health = "unknown"
    status_code, body = http_get(state["url"].rstrip("/") + "/health", timeout=2.0)
    if status_code == 200:
        health = "ok"
    elif status_code:
        health = f"http_{status_code}"

    print(f"Service is running: {state['url']}")
    print(f"pid={state['pid']} port={state['port']} health={health}")
    command = process_command(state["pid"])
    if command:
        print(f"command={command}")
    print(f"Log file: {LOG_FILE}")


def logs(args):
    if not LOG_FILE.exists():
        print("No logs are available.")
        return

    def print_tail():
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines[-args.lines:]:
            print(line)

    print_tail()
    if not args.follow:
        return

    position = LOG_FILE.stat().st_size
    try:
        while True:
            size = LOG_FILE.stat().st_size
            if size < position:
                position = 0
            if size > position:
                with LOG_FILE.open("r", encoding="utf-8", errors="replace") as f:
                    f.seek(position)
                    chunk = f.read()
                    if chunk:
                        print(chunk, end="")
                    position = f.tell()
            time.sleep(1)
    except KeyboardInterrupt:
        return


def open_app(_args):
    state = current_status()
    if not state["running"]:
        raise SystemExit("Service is not running. Run: python3 deploy.py start --open")
    webbrowser.open(state["url"])
    print(f"Opened: {state['url']}")


def build_parser():
    parser = argparse.ArgumentParser(description="Local deployment manager for the AI Agent Collaboration Workspace")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("setup", help="Install dependencies from requirements.txt")
    p.set_defaults(func=setup)

    p = sub.add_parser("start", help="Start the local service in the background")
    p.add_argument("--host", default=DEFAULT_HOST, help=f"Bind address (default: {DEFAULT_HOST})")
    p.add_argument("--base-port", type=int, default=DEFAULT_PORT, help=f"First port to scan (default: {DEFAULT_PORT})")
    p.add_argument("--port", type=int, default=None, help="Use a fixed port; startup fails if it is unavailable")
    p.add_argument("--timeout", type=float, default=45.0, help="Startup health-check timeout in seconds")
    p.add_argument("--open", action="store_true", help="Open the app in a browser after startup")
    p.add_argument("--no-caffeinate", action="store_true", help="Disable the macOS caffeinate wrapper")
    p.set_defaults(func=start)

    p = sub.add_parser("stop", help="Stop the local service")
    p.add_argument("--timeout", type=float, default=10.0, help="Graceful shutdown timeout in seconds")
    p.set_defaults(func=stop)

    p = sub.add_parser("restart", help="Restart the local service")
    p.add_argument("--host", default=DEFAULT_HOST, help=f"Bind address (default: {DEFAULT_HOST})")
    p.add_argument("--base-port", type=int, default=DEFAULT_PORT, help=f"First port to scan (default: {DEFAULT_PORT})")
    p.add_argument("--port", type=int, default=None, help="Use a fixed port; startup fails if it is unavailable")
    p.add_argument("--timeout", type=float, default=45.0, help="Startup health-check timeout in seconds")
    p.add_argument("--stop-timeout", type=float, default=10.0, help="Shutdown timeout in seconds")
    p.add_argument("--open", action="store_true", help="Open the app in a browser after restart")
    p.add_argument("--no-caffeinate", action="store_true", help="Disable the macOS caffeinate wrapper")
    p.set_defaults(func=restart)

    p = sub.add_parser("status", help="Show service status")
    p.set_defaults(func=status)

    p = sub.add_parser("logs", help="Show service logs")
    p.add_argument("-n", "--lines", type=int, default=80, help="Number of recent log lines to show")
    p.add_argument("-f", "--follow", action="store_true", help="Follow log output")
    p.set_defaults(func=logs)

    p = sub.add_parser("open", help="Open the running local app")
    p.set_defaults(func=open_app)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
