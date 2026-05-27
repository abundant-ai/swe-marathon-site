"""Supervisor: keeps all child processes alive, restarts on crash, stays in
the foreground. Receives SIGTERM/SIGINT and tears down children.

Children:
  - relay: cross-node broadcast TCP relay (port 8500, killable by grader)
  - node0/node1/node2: HTTP+WebSocket nodes on 127.0.0.1:8000-8002
  - irc: IRC gateway on 0.0.0.0:6667
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = sys.executable

CHILDREN = [
    {
        "name": "relay",
        "cmd": [PYTHON, "-m", "server.broadcast"],
        "env": {},
    },
    {
        "name": "node0",
        "cmd": [PYTHON, "-m", "server.http_app"],
        "env": {"HUDDLE_NODE_ID": "0", "HUDDLE_PORT": str(int(os.environ.get("HUDDLE_HTTP_BASE_PORT", "8000")) + 0)},
    },
    {
        "name": "node1",
        "cmd": [PYTHON, "-m", "server.http_app"],
        "env": {"HUDDLE_NODE_ID": "1", "HUDDLE_PORT": str(int(os.environ.get("HUDDLE_HTTP_BASE_PORT", "8000")) + 1)},
    },
    {
        "name": "node2",
        "cmd": [PYTHON, "-m", "server.http_app"],
        "env": {"HUDDLE_NODE_ID": "2", "HUDDLE_PORT": str(int(os.environ.get("HUDDLE_HTTP_BASE_PORT", "8000")) + 2)},
    },
    {
        "name": "irc",
        "cmd": [PYTHON, "-m", "server.irc_gateway"],
        "env": {},
    },
]


def spawn(child: dict) -> subprocess.Popen:
    env = os.environ.copy()
    env.update(child["env"])
    env["PYTHONPATH"] = ROOT
    proc = subprocess.Popen(
        child["cmd"],
        env=env,
        cwd=ROOT,
        stdout=sys.stdout,
        stderr=sys.stderr,
        preexec_fn=os.setsid,
    )
    print(f"[supervisor] started {child['name']} pid={proc.pid}", flush=True)
    return proc


def main() -> None:
    procs: dict[str, subprocess.Popen] = {}
    last_restart: dict[str, float] = {}
    shutdown = False

    def stop_all(*_):
        nonlocal shutdown
        shutdown = True
        for name, p in list(procs.items()):
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
        # give them a moment, then SIGKILL
        deadline = time.time() + 5
        while time.time() < deadline and any(p.poll() is None for p in procs.values()):
            time.sleep(0.1)
        for name, p in procs.items():
            if p.poll() is None:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop_all)
    signal.signal(signal.SIGINT, stop_all)

    for child in CHILDREN:
        procs[child["name"]] = spawn(child)
        last_restart[child["name"]] = time.time()
        time.sleep(0.05)

    while not shutdown:
        for child in CHILDREN:
            p = procs.get(child["name"])
            if p is None or p.poll() is not None:
                rc = p.returncode if p else "?"
                print(f"[supervisor] {child['name']} exited rc={rc}; respawning", flush=True)
                # backoff if it just crashed
                age = time.time() - last_restart.get(child["name"], 0)
                if age < 2.0:
                    time.sleep(2.0 - age)
                procs[child["name"]] = spawn(child)
                last_restart[child["name"]] = time.time()
        time.sleep(0.5)


if __name__ == "__main__":
    main()
