"""Cross-node fan-out broker.

A single TCP server on 127.0.0.1:9100. Each HTTP node (and the IRC gateway)
keeps a TCP connection to the broker; published frames are rebroadcast to
every other connection. The broker is intentionally simple — when it dies,
the supervisor restarts it. While it is down, the nodes fall back to
polling the ``events`` table in SQLite (see node.py) so cross-node fan-out
still works within a few seconds.

Frame format on the wire: each line is a JSON object terminated by ``\n``.
Encoding: UTF-8.
"""
import asyncio
import os
import sys


BROKER_HOST = os.environ.get("HUDDLE_BROKER_HOST", "127.0.0.1")
BROKER_PORT = int(os.environ.get("HUDDLE_BROKER_PORT", "9100"))


class Broker:
    def __init__(self):
        self.clients = set()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.clients.add(writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                # Fan out raw line to every other client.
                stale = []
                for w in list(self.clients):
                    if w is writer:
                        continue
                    try:
                        w.write(line)
                    except Exception:
                        stale.append(w)
                for w in stale:
                    self.clients.discard(w)
                    try:
                        w.close()
                    except Exception:
                        pass
                # drain best-effort
                for w in list(self.clients):
                    if w is writer:
                        continue
                    try:
                        await w.drain()
                    except Exception:
                        self.clients.discard(w)
        except Exception:
            pass
        finally:
            self.clients.discard(writer)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


async def main():
    b = Broker()
    server = await asyncio.start_server(b.handle, BROKER_HOST, BROKER_PORT)
    print(f"[broker] listening on {BROKER_HOST}:{BROKER_PORT}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
