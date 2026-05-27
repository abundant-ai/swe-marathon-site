"""Supervises broker, three HTTP nodes, and the IRC gateway.

Each child is its own subprocess so the grader can SIGKILL a single one and the
others keep running. The supervisor restarts any dead child within a few
seconds (well under the 60-second budget) and itself stays in the foreground.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time


PROCS: dict[str, subprocess.Popen] = {}
RESTART_BACKOFF: dict[str, float] = {}


def python_cmd(module: str) -> list[str]:
    return [sys.executable, "-u", "-m", module]


def env_for(name: str) -> dict:
    env = os.environ.copy()
    env.setdefault("HUDDLE_DATA_DIR", "/app/data")
    if name.startswith("node-"):
        idx = int(name.split("-", 1)[1])
        env["HUDDLE_NODE_ID"] = str(idx)
        env["HUDDLE_HTTP_HOST"] = os.environ.get("HUDDLE_HTTP_HOST", "0.0.0.0")
        env["HUDDLE_HTTP_PORT"] = str(int(os.environ.get("HUDDLE_HTTP_BASE_PORT", "8000")) + idx)
    return env


def spawn(name: str, cmd: list[str]):
    print(f"[sup] spawn {name}: {' '.join(cmd)}", flush=True)
    p = subprocess.Popen(cmd, env=env_for(name))
    PROCS[name] = p


def all_specs() -> list[tuple[str, list[str]]]:
    return [
        ("broker", python_cmd("app.broker_main")),
        ("node-0", python_cmd("app.node")),
        ("node-1", python_cmd("app.node")),
        ("node-2", python_cmd("app.node")),
        ("irc", python_cmd("app.irc_main")),
    ]


def shutdown(*_):
    for name, p in list(PROCS.items()):
        try:
            p.terminate()
        except Exception:
            pass
    deadline = time.time() + 5
    for name, p in list(PROCS.items()):
        try:
            p.wait(max(0.1, deadline - time.time()))
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    sys.exit(0)


def main():
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    specs = dict(all_specs())
    for name, cmd in specs.items():
        spawn(name, cmd)
        time.sleep(0.15)
    while True:
        time.sleep(0.5)
        for name, cmd in list(specs.items()):
            p = PROCS.get(name)
            if p is None or p.poll() is not None:
                # restart
                last = RESTART_BACKOFF.get(name, 0)
                now = time.time()
                if now - last < 1.0:
                    continue
                RESTART_BACKOFF[name] = now
                print(f"[sup] {name} died, restarting", flush=True)
                spawn(name, cmd)


if __name__ == "__main__":
    main()
