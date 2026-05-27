"""Supervisor: spawns and restarts the broker, three HTTP nodes, and the IRC gateway.

Runs in the foreground; exits on SIGTERM/SIGINT.
"""
import os
import signal
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

PROCS = {}
SHUTTING_DOWN = False


def spawn(name, args, env=None):
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    p = subprocess.Popen(
        [PY, "-u", "-m"] + args,
        cwd=ROOT,
        env=full_env,
    )
    PROCS[name] = (p, args, env or {}, time.time())
    print(f"[sup] spawned {name} pid={p.pid}", flush=True)


def shutdown(*_):
    global SHUTTING_DOWN
    SHUTTING_DOWN = True
    for name, (p, _a, _e, _t) in PROCS.items():
        try:
            p.terminate()
        except Exception:
            pass
    deadline = time.time() + 3
    while time.time() < deadline:
        if all(p.poll() is not None for (p, _a, _e, _t) in PROCS.values()):
            break
        time.sleep(0.1)
    for name, (p, _a, _e, _t) in PROCS.items():
        if p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass
    sys.exit(0)


def main():
    # Initialize DB schema once before any worker starts.
    from server import store
    store.init_schema()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Broker first.
    spawn("broker", ["server.broker"])
    time.sleep(0.2)
    # Three HTTP nodes.
    for i in range(3):
        base_port = int(os.environ.get("HUDDLE_HTTP_BASE_PORT", "8000"))
        port = base_port + i
        spawn(f"node{i}", ["server.node", str(i), str(port)])
    # IRC gateway.
    spawn("irc", ["server.irc"])

    # Restart any process that exits unexpectedly.
    while not SHUTTING_DOWN:
        time.sleep(0.5)
        for name, (p, args, env, started) in list(PROCS.items()):
            rc = p.poll()
            if rc is not None:
                if SHUTTING_DOWN:
                    return
                # Backoff if the process flaps.
                age = time.time() - started
                if age < 1:
                    time.sleep(0.5)
                print(f"[sup] {name} exited rc={rc}, restarting", flush=True)
                spawn(name, args, env)


if __name__ == "__main__":
    main()
