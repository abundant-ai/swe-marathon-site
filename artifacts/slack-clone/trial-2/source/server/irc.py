"""IRC gateway listening on 0.0.0.0:6667.

PASS-authenticated with a workspace bearer token. The gateway picks up
the user's bearer token via the ``PASS`` command and uses it the same way
the HTTP layer does — every IRC client maps to a logged-in user.

PRIVMSGs from IRC are committed via the same write path as the HTTP
``POST /api/channels/{id}/messages`` (so they share the per-channel
``seq``). Web messages fan back out to subscribed IRC clients via the
shared :class:`Bus`.
"""
import asyncio
import os
import re
import socket
import sys

from . import store
from .bus import Bus
from .events import emit_durable, record_event, publish_event


SERVER_NAME = "huddle.local"
HOST = "0.0.0.0"
PORT = int(os.environ.get("HUDDLE_IRC_PORT", "6667"))


class IRCClient:
    def __init__(self, server, reader, writer):
        self.server: IRCServer = server
        self.reader = reader
        self.writer = writer
        self.peer = writer.get_extra_info("peername")
        self.password: str | None = None
        self.nick: str | None = None
        self.user_real: str | None = None
        self.registered = False
        self.user_row = None
        self.token = None
        self.joined: dict[int, str] = {}  # channel_id -> #name
        self.alive = True

    def addr_prefix(self) -> str:
        host = self.peer[0] if self.peer else "irc"
        return f"{self.nick or '*'}!~{self.user_real or self.nick or 'user'}@{host}"

    async def send(self, line: str):
        try:
            self.writer.write((line + "\r\n").encode("utf-8", errors="replace"))
            await self.writer.drain()
        except Exception:
            self.alive = False

    async def numeric(self, n: int, *parts: str):
        # ":server NNN nick :text"
        nick = self.nick or "*"
        body = " ".join(parts) if parts else ""
        await self.send(f":{SERVER_NAME} {n:03d} {nick} {body}")

    async def disconnect(self):
        self.alive = False
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass

    # ---- registration ----

    async def maybe_register(self):
        if self.registered:
            return
        if not (self.password and self.nick and self.user_real):
            return
        # Validate token
        conn = store.connect()
        try:
            user = store.auth_user(conn, self.password)
        finally:
            conn.close()
        if not user:
            await self.numeric(464, ":Password incorrect")
            await self.disconnect()
            return
        self.user_row = user
        self.token = self.password
        # nick collision: enforce uniqueness within active sessions
        for c in self.server.clients:
            if c is self: continue
            if c.registered and c.nick and c.nick.lower() == self.nick.lower():
                await self.numeric(433, self.nick, ":Nickname is already in use")
                # don't close
                self.nick = None
                return
        # persist nick mapping
        conn = store.connect()
        try:
            row = conn.execute("SELECT * FROM irc_nicks WHERE nick=? COLLATE NOCASE", (self.nick,)).fetchone()
            if row and row["user_id"] != user["id"]:
                await self.numeric(433, self.nick, ":Nickname is already in use")
                self.nick = None
                return
            conn.execute("INSERT OR REPLACE INTO irc_nicks (user_id, nick) VALUES (?, ?)", (user["id"], self.nick))
        finally:
            conn.close()
        self.registered = True
        await self.numeric(1, f":Welcome to Huddle IRC, {self.nick}!")
        await self.numeric(2, f":Your host is {SERVER_NAME}")
        await self.numeric(3, ":This server was created today")
        await self.numeric(4, SERVER_NAME, "huddle-1.0", "o", "o")
        await self.numeric(5, "PREFIX=(o)@", "CHANTYPES=#", "NETWORK=Huddle", ":are supported")
        await self.numeric(376, ":End of MOTD")

    # ---- handlers ----

    async def handle_line(self, line: str):
        line = line.strip()
        if not line:
            return
        # parse simple IRC: optional :prefix CMD args :trailing
        if line.startswith(":"):
            sp = line.find(" ")
            line = line[sp+1:].lstrip() if sp >= 0 else ""
        # extract trailing
        trailing = None
        sp = line.find(" :")
        if sp >= 0:
            trailing = line[sp+2:]
            line = line[:sp]
        parts = line.split()
        if not parts:
            return
        cmd = parts[0].upper()
        args = parts[1:]
        if trailing is not None:
            args.append(trailing)
        await self.dispatch(cmd, args)

    async def dispatch(self, cmd, args):
        if cmd == "PASS":
            self.password = args[0] if args else ""
            return
        if cmd == "NICK":
            if not args:
                await self.numeric(431, ":No nickname given")
                return
            new = args[0]
            if not re.match(r"^[A-Za-z\[\]\\`_^{|}][A-Za-z0-9\[\]\\`_^{|}-]{0,31}$", new):
                await self.numeric(432, new, ":Erroneous nickname")
                return
            # collision check (across active sessions)
            for c in self.server.clients:
                if c is self: continue
                if c.registered and c.nick and c.nick.lower() == new.lower():
                    await self.numeric(433, new, ":Nickname is already in use")
                    return
            self.nick = new
            await self.maybe_register()
            return
        if cmd == "USER":
            self.user_real = args[0] if args else "irc"
            await self.maybe_register()
            return
        if cmd == "PING":
            tok = args[0] if args else SERVER_NAME
            await self.send(f":{SERVER_NAME} PONG {SERVER_NAME} :{tok}")
            return
        if cmd == "PONG":
            return
        if cmd == "QUIT":
            try:
                msg = args[0] if args else "Client Quit"
                await self.send(f"ERROR :Closing Link: {msg}")
            except Exception:
                pass
            await self.cleanup_after_quit()
            await self.disconnect()
            return
        if not self.registered:
            await self.numeric(451, ":You have not registered")
            return
        if cmd == "JOIN":
            target = args[0] if args else ""
            await self.handle_join(target)
            return
        if cmd == "PART":
            target = args[0] if args else ""
            await self.handle_part(target)
            return
        if cmd == "NAMES":
            target = args[0] if args else ""
            await self.handle_names(target)
            return
        if cmd == "WHO":
            target = args[0] if args else ""
            await self.handle_who(target)
            return
        if cmd == "LIST":
            await self.handle_list()
            return
        if cmd == "TOPIC":
            target = args[0] if args else ""
            new_topic = args[1] if len(args) > 1 else None
            await self.handle_topic(target, new_topic)
            return
        if cmd == "MODE":
            target = args[0] if args else ""
            await self.send(f":{SERVER_NAME} MODE {target} +nt")
            return
        if cmd == "PRIVMSG":
            target = args[0] if args else ""
            text = args[1] if len(args) > 1 else ""
            await self.handle_privmsg(target, text)
            return
        await self.numeric(421, cmd, ":Unknown command")

    async def cleanup_after_quit(self):
        # leave all rooms (other clients should see QUIT). Keep simple.
        for cid in list(self.joined):
            chname = self.joined.pop(cid)
            await self.server.fanout_local(cid, f":{self.addr_prefix()} PART {chname} :Quit")
        await self.send_quit_to_all()

    async def send_quit_to_all(self):
        # we don't track membership broadcasts, so just leave silently
        pass

    # ---- channel ops ----

    def _resolve_channel(self, target: str):
        """Map ``#channel`` or ``#workspace.channel`` to a (workspace, channel) pair."""
        if not target.startswith("#"):
            return None, None
        name = target[1:]
        conn = store.connect()
        try:
            chan = None
            ws = None
            if "." in name:
                ws_slug, ch_name = name.split(".", 1)
                ws = conn.execute("SELECT * FROM workspaces WHERE slug=?", (ws_slug,)).fetchone()
                if ws:
                    chan = conn.execute(
                        "SELECT * FROM channels WHERE workspace_id=? AND name=?",
                        (ws["id"], ch_name),
                    ).fetchone()
            else:
                # search across user's workspaces
                ws_list = conn.execute(
                    "SELECT w.* FROM workspaces w JOIN workspace_members m ON m.workspace_id=w.id WHERE m.user_id=?",
                    (self.user_row["id"],),
                ).fetchall()
                for w in ws_list:
                    c = conn.execute(
                        "SELECT * FROM channels WHERE workspace_id=? AND name=?",
                        (w["id"], name),
                    ).fetchone()
                    if c:
                        ws = w
                        chan = c
                        break
            return ws, chan
        finally:
            conn.close()

    async def handle_join(self, target):
        ws, chan = self._resolve_channel(target)
        if not chan:
            await self.numeric(403, target, ":No such channel")
            return
        # Membership check / auto-join logic mirrors HTTP join
        conn = store.connect()
        try:
            role = store.workspace_role(conn, chan["workspace_id"], self.user_row["id"])
            if chan["is_private"]:
                if not role or not store.is_channel_member(conn, chan["id"], self.user_row["id"]):
                    await self.numeric(403, target, ":Cannot join (private)")
                    return
            else:
                if not role:
                    if ws["join_mode"] != "open":
                        await self.numeric(403, target, ":Forbidden")
                        return
                    conn.execute(
                        "INSERT OR IGNORE INTO workspace_members (workspace_id, user_id, role, joined_at) VALUES (?, ?, 'member', ?)",
                        (chan["workspace_id"], self.user_row["id"], store.now_iso()),
                    )
                if not store.is_channel_member(conn, chan["id"], self.user_row["id"]):
                    conn.execute(
                        "INSERT INTO channel_members (channel_id, user_id, joined_at, last_read_seq) VALUES (?, ?, ?, 0)",
                        (chan["id"], self.user_row["id"], store.now_iso()),
                    )
            chan = conn.execute("SELECT * FROM channels WHERE id=?", (chan["id"],)).fetchone()
        finally:
            conn.close()
        chname = "#" + chan["name"]
        self.joined[chan["id"]] = chname
        # Tell the client we joined
        await self.send(f":{self.addr_prefix()} JOIN {chname}")
        if chan["topic"]:
            await self.numeric(332, chname, f":{chan['topic']}")
        else:
            await self.numeric(331, chname, ":No topic is set")
        # NAMES list
        names = self._channel_nicks(chan["id"])
        chunk = " ".join(names)
        await self.numeric(353, "=", chname, f":{chunk}")
        await self.numeric(366, chname, ":End of /NAMES list")
        # emit member.joined
        await emit_durable(self.server.bus, chan["id"], "member.joined", {"user_id": self.user_row["id"]})

    async def handle_part(self, target):
        ws, chan = self._resolve_channel(target)
        if not chan:
            await self.numeric(403, target, ":No such channel")
            return
        chname = "#" + chan["name"]
        self.joined.pop(chan["id"], None)
        await self.send(f":{self.addr_prefix()} PART {chname}")

    async def handle_names(self, target):
        ws, chan = self._resolve_channel(target)
        if not chan:
            await self.numeric(366, target or "*", ":End of /NAMES list")
            return
        chname = "#" + chan["name"]
        names = self._channel_nicks(chan["id"])
        chunk = " ".join(names)
        await self.numeric(353, "=", chname, f":{chunk}")
        await self.numeric(366, chname, ":End of /NAMES list")

    async def handle_who(self, target):
        ws, chan = self._resolve_channel(target)
        if not chan:
            await self.numeric(315, target or "*", ":End of /WHO list")
            return
        chname = "#" + chan["name"]
        for nick in self._channel_nicks(chan["id"]):
            await self.numeric(352, chname, "user", "host", SERVER_NAME, nick, "H", ":0 user")
        await self.numeric(315, chname, ":End of /WHO list")

    async def handle_list(self):
        await self.numeric(321, "Channel", ":Users  Name")
        conn = store.connect()
        try:
            rows = conn.execute(
                "SELECT c.*, w.slug AS ws_slug FROM channels c JOIN workspaces w ON w.id=c.workspace_id "
                "JOIN workspace_members m ON m.workspace_id=c.workspace_id "
                "WHERE m.user_id=? AND c.is_archived=0 AND c.is_dm=0",
                (self.user_row["id"],),
            ).fetchall()
        finally:
            conn.close()
        for r in rows:
            await self.numeric(322, "#" + r["name"], "0", ":" + (r["topic"] or ""))
        await self.numeric(323, ":End of /LIST")

    async def handle_topic(self, target, new_topic):
        ws, chan = self._resolve_channel(target)
        if not chan:
            await self.numeric(403, target, ":No such channel")
            return
        chname = "#" + chan["name"]
        if new_topic is None:
            if chan["topic"]:
                await self.numeric(332, chname, f":{chan['topic']}")
            else:
                await self.numeric(331, chname, ":No topic is set")
            return
        # set topic
        conn = store.connect()
        try:
            role = store.workspace_role(conn, chan["workspace_id"], self.user_row["id"])
            if role not in ("owner", "admin"):
                await self.numeric(482, chname, ":You're not channel operator")
                return
            conn.execute("UPDATE channels SET topic=? WHERE id=?", (new_topic[:250], chan["id"]))
            chan = conn.execute("SELECT * FROM channels WHERE id=?", (chan["id"],)).fetchone()
        finally:
            conn.close()
        await emit_durable(self.server.bus, chan["id"], "channel.updated", {"channel": store.channel_to_obj(chan)})
        await self.send(f":{self.addr_prefix()} TOPIC {chname} :{new_topic[:250]}")

    async def handle_privmsg(self, target, text):
        if not target.startswith("#"):
            await self.numeric(401, target, ":No such nick/channel")
            return
        ws, chan = self._resolve_channel(target)
        if not chan:
            await self.numeric(403, target, ":No such channel")
            return
        if chan["id"] not in self.joined:
            # auto-join semantics on PRIVMSG to be lenient
            await self.handle_join(target)
            if chan["id"] not in self.joined:
                return
        # commit through the unified write path
        conn = store.connect()
        try:
            if chan["is_archived"]:
                await self.numeric(404, target, ":Cannot send to archived channel")
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                seq = store.reserve_seq(conn, chan["id"])
                cur = conn.execute(
                    "INSERT INTO messages (channel_id, author_id, body, parent_id, reply_count, created_at, seq) VALUES (?, ?, ?, NULL, 0, ?, ?)",
                    (chan["id"], self.user_row["id"], text, store.now_iso(), seq),
                )
                mid = cur.lastrowid
                # mentions
                mentioned = store.parse_mentions(conn, chan["workspace_id"], self.user_row["id"], text)
                for u in mentioned:
                    conn.execute("INSERT OR IGNORE INTO mentions (message_id, user_id) VALUES (?, ?)", (mid, u))
                m_row = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
                msg_obj = store.message_to_obj(conn, m_row)
                record_event(conn, chan["id"], seq, "message.new", {"message": msg_obj})
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        await publish_event(self.server.bus, chan["id"], seq, "message.new", {"message": msg_obj})

    def _channel_nicks(self, channel_id):
        conn = store.connect()
        try:
            rows = conn.execute(
                "SELECT u.username, n.nick FROM channel_members cm JOIN users u ON u.id=cm.user_id "
                "LEFT JOIN irc_nicks n ON n.user_id=u.id WHERE cm.channel_id=?",
                (channel_id,),
            ).fetchall()
        finally:
            conn.close()
        return [(r["nick"] or r["username"]) for r in rows]


class IRCServer:
    def __init__(self):
        self.clients: set[IRCClient] = set()
        self.bus = Bus(node_id=99)

    def deliver(self, frame):
        # only message.new bridges to IRC
        cid = frame.get("channel_id")
        kind = frame.get("type")
        if kind != "message.new":
            return
        msg = frame.get("message") or {}
        body = msg.get("body") or ""
        author = msg.get("author") or {}
        author_id = msg.get("author_id")
        # Look up channel name once
        conn = store.connect()
        try:
            chan = conn.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
            nick_row = conn.execute("SELECT nick FROM irc_nicks WHERE user_id=?", (author_id,)).fetchone()
        finally:
            conn.close()
        if not chan:
            return
        nick = (nick_row["nick"] if nick_row else None) or (author.get("username") or "user")
        line = f":{nick}!~{nick}@huddle PRIVMSG #{chan['name']} :{body}"
        for c in list(self.clients):
            if not c.alive or not c.registered:
                continue
            if cid not in c.joined:
                continue
            # Don't echo back to the original IRC sender (they already have the message)
            if c.user_row and c.user_row["id"] == author_id:
                continue
            try:
                asyncio.create_task(c.send(line))
            except Exception:
                pass

    async def fanout_local(self, channel_id, line: str):
        for c in list(self.clients):
            if c.alive and channel_id in c.joined:
                try:
                    await c.send(line)
                except Exception:
                    pass

    async def handle_client(self, reader, writer):
        c = IRCClient(self, reader, writer)
        self.clients.add(c)
        try:
            while c.alive:
                line = await reader.readline()
                if not line:
                    break
                try:
                    text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                except Exception:
                    continue
                try:
                    await c.handle_line(text)
                except Exception as e:
                    print(f"[irc] error: {e}", flush=True)
        except Exception:
            pass
        finally:
            self.clients.discard(c)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def run(self):
        await self.bus.start()
        self.bus.add_listener(self.deliver)
        server = await asyncio.start_server(self.handle_client, HOST, PORT)
        print(f"[irc] listening on {HOST}:{PORT}", flush=True)
        async with server:
            await server.serve_forever()


def main():
    s = IRCServer()
    try:
        asyncio.run(s.run())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
