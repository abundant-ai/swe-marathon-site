"""Minimal IRC gateway bridging an authenticated workspace into IRC.

Each TCP connection presents a single user (selected by `PASS <token>`).
Joins map to channels in the user's first owned/membered workspace whose name
matches; PRIVMSG into the same per-channel `seq` stream as the HTTP API.
Web messages fan out to IRC clients via the same DB-tail loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Optional

from . import db, util


IRC_HOST = os.environ.get("HUDDLE_IRC_HOST", "0.0.0.0")
IRC_PORT = int(os.environ.get("HUDDLE_IRC_PORT", "6667"))
SERVER_NAME = "huddle.irc"


# --- Helpers ---------------------------------------------------------------

def _send(writer: asyncio.StreamWriter, line: str):
    if not line.endswith("\r\n"):
        line += "\r\n"
    try:
        writer.write(line.encode("utf-8", errors="replace"))
    except Exception:
        pass


def _user_by_token(token: str) -> Optional[dict]:
    row = db.conn().execute(
        "SELECT u.* FROM tokens t JOIN users u ON u.id = t.user_id WHERE t.token = ?",
        (token,),
    ).fetchone()
    return dict(row) if row else None


def _user_workspaces(user_id: str) -> list[dict]:
    rows = db.conn().execute(
        "SELECT w.* FROM workspaces w JOIN workspace_members m ON m.workspace_id = w.id "
        "WHERE m.user_id = ? ORDER BY w.created_at ASC",
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _channel_by_name(workspace_id: str, name: str) -> Optional[dict]:
    name = name.lstrip("#").lower()
    row = db.conn().execute(
        "SELECT * FROM channels WHERE workspace_id = ? AND name = ?",
        (workspace_id, name),
    ).fetchone()
    return dict(row) if row else None


# --- Session ---------------------------------------------------------------

class IrcSession:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 server: "IrcGateway"):
        self.reader = reader
        self.writer = writer
        self.server = server
        peer = writer.get_extra_info("peername")
        self.host = (peer[0] if peer else "irc")
        self.token: Optional[str] = None
        self.user: Optional[dict] = None
        self.nick: str = ""
        self.user_set: bool = False
        self.registered: bool = False
        self.workspace: Optional[dict] = None
        self.joined: dict[str, dict] = {}  # name -> channel row
        self._closed = False

    def prefix(self) -> str:
        return f":{self.nick}!{(self.user['username'] if self.user else 'h')}@{self.host}"

    def send(self, line: str):
        _send(self.writer, line)

    def numeric(self, code: str, args: str):
        self.send(f":{SERVER_NAME} {code} {self.nick or '*'} {args}")

    async def run(self):
        self.send(f":{SERVER_NAME} NOTICE * :*** Huddle IRC gateway")
        try:
            while not self._closed:
                line = await self.reader.readline()
                if not line:
                    break
                line = line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    continue
                await self.handle_line(line)
        except Exception:
            pass
        finally:
            self.server.remove(self)
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass

    async def handle_line(self, line: str):
        # Parse: optional ":prefix" then COMMAND args... [:trailing]
        if line.startswith(":"):
            _, _, rest = line.partition(" ")
            line = rest
        cmd = ""
        args: list[str] = []
        rest = line
        if " :" in rest:
            head, _, trailing = rest.partition(" :")
            parts = head.split()
            args = parts[1:] if parts else []
            cmd = parts[0].upper() if parts else ""
            args.append(trailing)
        else:
            parts = rest.split()
            if parts:
                cmd = parts[0].upper()
                args = parts[1:]
        if not cmd:
            return
        if cmd == "PASS":
            self.token = args[0] if args else ""
            return
        if cmd == "NICK":
            new_nick = args[0] if args else ""
            if not self.token:
                self.numeric("464", ":Password required")
                return
            if not self.user:
                u = _user_by_token(self.token)
                if not u:
                    self.numeric("464", ":Bad password")
                    return
                self.user = u
                ws_list = _user_workspaces(u["id"])
                if ws_list:
                    self.workspace = ws_list[0]
            # Detect collision: existing session with same nick on same workspace
            for s in self.server.sessions:
                if s is self:
                    continue
                if s.nick.lower() == new_nick.lower() and s.workspace and self.workspace and \
                        s.workspace["id"] == self.workspace["id"]:
                    self.numeric("433", f"{new_nick} :Nickname is already in use.")
                    return
            self.nick = new_nick
            await self._maybe_register()
            return
        if cmd == "USER":
            self.user_set = True
            await self._maybe_register()
            return
        if not self.registered:
            # Most commands require registration.
            if cmd not in ("CAP", "QUIT", "PASS"):
                self.numeric("451", ":You have not registered")
                return
        if cmd == "CAP":
            # No capability negotiation; ack req with NAK so registration can proceed.
            sub = args[0].upper() if args else ""
            if sub == "LS":
                self.send(f":{SERVER_NAME} CAP * LS :")
            elif sub == "REQ":
                want = args[1] if len(args) > 1 else ""
                self.send(f":{SERVER_NAME} CAP * NAK :{want}")
            elif sub == "END":
                pass
            return
        if cmd == "PING":
            payload = args[0] if args else SERVER_NAME
            self.send(f":{SERVER_NAME} PONG {SERVER_NAME} :{payload}")
            return
        if cmd == "PONG":
            return
        if cmd == "QUIT":
            self._closed = True
            return
        if cmd == "JOIN":
            if not args:
                return
            for chan_arg in args[0].split(","):
                await self._join(chan_arg)
            return
        if cmd == "PART":
            if not args:
                return
            for chan_arg in args[0].split(","):
                self._part(chan_arg)
            return
        if cmd == "NAMES":
            if not args:
                return
            for chan_arg in args[0].split(","):
                self._names(chan_arg)
            return
        if cmd == "WHO":
            target = args[0] if args else ""
            self.numeric("315", f"{target} :End of /WHO list")
            return
        if cmd == "LIST":
            self._list()
            return
        if cmd == "TOPIC":
            if not args:
                return
            chan = args[0]
            new_topic = args[1] if len(args) > 1 else None
            await self._topic(chan, new_topic)
            return
        if cmd == "MODE":
            target = args[0] if args else ""
            if target.startswith("#"):
                self.numeric("324", f"{target} +n")
            return
        if cmd == "PRIVMSG":
            if len(args) < 2:
                return
            await self._privmsg(args[0], args[1])
            return
        # Unknown
        self.numeric("421", f"{cmd} :Unknown command")

    async def _maybe_register(self):
        if self.registered:
            return
        if not self.user or not self.nick or not self.user_set:
            return
        self.registered = True
        ws_label = (self.workspace["name"] if self.workspace else "Huddle")
        self.numeric("001", f":Welcome to {ws_label}, {self.nick}")
        self.numeric("002", f":Your host is {SERVER_NAME}")
        self.numeric("003", ":This server was created today")
        self.numeric("004", f"{SERVER_NAME} 1.0 i tnp")
        self.numeric("005", f"PREFIX=(o)@ CHANTYPES=# CASEMAPPING=ascii :are supported by this server")
        self.numeric("375", f":- {SERVER_NAME} Message of the day -")
        self.numeric("372", f":- Welcome to the Huddle IRC bridge.")
        self.numeric("376", ":End of /MOTD command")
        self.server.add(self)

    async def _join(self, chan_name: str):
        if not chan_name.startswith("#"):
            self.numeric("403", f"{chan_name} :No such channel")
            return
        if not self.workspace:
            self.numeric("403", f"{chan_name} :No such channel")
            return
        ch = _channel_by_name(self.workspace["id"], chan_name)
        if not ch:
            self.numeric("403", f"{chan_name} :No such channel")
            return
        # Add channel membership in DB.
        try:
            db.conn().execute("BEGIN IMMEDIATE")
            db.conn().execute(
                "INSERT OR IGNORE INTO channel_members(channel_id, user_id, joined_at, last_read_seq) "
                "VALUES (?, ?, ?, 0)",
                (ch["id"], self.user["id"], db.now_ms()),
            )
            db.conn().execute("COMMIT")
        except Exception:
            db.tx_rollback()
        self.joined[ch["name"]] = ch
        # Send JOIN, TOPIC, NAMES.
        self.send(f"{self.prefix()} JOIN :#{ch['name']}")
        if ch["topic"]:
            self.numeric("332", f"#{ch['name']} :{ch['topic']}")
        else:
            self.numeric("331", f"#{ch['name']} :No topic is set")
        names = []
        for r in db.conn().execute(
            "SELECT u.username FROM channel_members cm JOIN users u ON u.id = cm.user_id "
            "WHERE cm.channel_id = ?",
            (ch["id"],),
        ).fetchall():
            names.append(r["username"])
        self.numeric("353", f"= #{ch['name']} :{' '.join(names)}")
        self.numeric("366", f"#{ch['name']} :End of NAMES")

    def _part(self, chan_name: str):
        name = chan_name.lstrip("#").lower()
        if name in self.joined:
            self.send(f"{self.prefix()} PART #{name}")
            del self.joined[name]

    def _names(self, chan_name: str):
        name = chan_name.lstrip("#").lower()
        ch = self.joined.get(name) or (self.workspace and _channel_by_name(self.workspace["id"], name))
        if not ch:
            self.numeric("366", f"#{name} :End of NAMES")
            return
        names = []
        for r in db.conn().execute(
            "SELECT u.username FROM channel_members cm JOIN users u ON u.id = cm.user_id "
            "WHERE cm.channel_id = ?",
            (ch["id"],),
        ).fetchall():
            names.append(r["username"])
        self.numeric("353", f"= #{ch['name']} :{' '.join(names)}")
        self.numeric("366", f"#{ch['name']} :End of NAMES")

    def _list(self):
        self.numeric("321", "Channel :Users  Name")
        if self.workspace:
            for r in db.conn().execute(
                "SELECT name, topic FROM channels WHERE workspace_id = ? AND is_dm = 0 AND is_archived = 0",
                (self.workspace["id"],),
            ).fetchall():
                self.numeric("322", f"#{r['name']} 0 :{r['topic']}")
        self.numeric("323", ":End of /LIST")

    async def _topic(self, chan_name: str, new_topic: Optional[str]):
        name = chan_name.lstrip("#").lower()
        if not self.workspace:
            return
        ch = _channel_by_name(self.workspace["id"], name)
        if not ch:
            self.numeric("403", f"#{name} :No such channel")
            return
        if new_topic is None:
            if ch["topic"]:
                self.numeric("332", f"#{ch['name']} :{ch['topic']}")
            else:
                self.numeric("331", f"#{ch['name']} :No topic is set")
            return
        try:
            db.conn().execute("BEGIN IMMEDIATE")
            db.conn().execute("UPDATE channels SET topic = ? WHERE id = ?", (new_topic[:250], ch["id"]))
            updated = db.conn().execute("SELECT * FROM channels WHERE id = ?", (ch["id"],)).fetchone()
            db.commit_event(ch["id"], "channel.updated", {"channel": util.public_channel(updated)})
            db.conn().execute("COMMIT")
        except Exception:
            db.tx_rollback()
        self.send(f"{self.prefix()} TOPIC #{ch['name']} :{new_topic[:250]}")

    async def _privmsg(self, target: str, body: str):
        if not target or not body:
            return
        if not target.startswith("#"):
            return
        if not self.workspace:
            self.numeric("403", f"{target} :No such channel")
            return
        ch = _channel_by_name(self.workspace["id"], target)
        if not ch:
            self.numeric("403", f"{target} :No such channel")
            return
        if ch["is_archived"]:
            self.numeric("404", f"{target} :Channel is archived")
            return
        # Insert message and event in a single transaction.
        try:
            db.conn().execute("BEGIN IMMEDIATE")
            mid = db.gen_id()
            now = db.now_ms()
            eid, seq = db.commit_event(ch["id"], "__placeholder__", {})
            db.conn().execute(
                "INSERT INTO messages(id, channel_id, author_id, body, parent_id, created_at, edited_at, deleted, seq, reply_count) "
                "VALUES (?, ?, ?, ?, NULL, ?, NULL, 0, ?, 0)",
                (mid, ch["id"], self.user["id"], body, now, seq),
            )
            mentions = util.resolve_mentions(ch["workspace_id"], ch["id"], body, self.user["id"])
            for uid in mentions:
                db.conn().execute(
                    "INSERT OR IGNORE INTO message_mentions(message_id, user_id) VALUES (?, ?)",
                    (mid, uid),
                )
            msg_row = db.conn().execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
            obj = util.public_message(msg_row)
            db.conn().execute(
                "UPDATE events SET kind = ?, payload = ? WHERE id = ?",
                ("message.new", json.dumps({"message": obj}), eid),
            )
            db.conn().execute("COMMIT")
        except Exception:
            db.tx_rollback()
            self.numeric("404", f"{target} :Cannot send to channel")
            return


# --- Server ----------------------------------------------------------------

class IrcGateway:
    def __init__(self):
        self.sessions: list[IrcSession] = []
        self.last_event_id: int = 0

    def add(self, s: IrcSession):
        if s not in self.sessions:
            self.sessions.append(s)

    def remove(self, s: IrcSession):
        try:
            self.sessions.remove(s)
        except ValueError:
            pass

    async def fan_out_loop(self):
        while True:
            try:
                await asyncio.sleep(0.25)
                evts = db.fetch_events_after_id(self.last_event_id, limit=500)
                for e in evts:
                    if e["id"] <= self.last_event_id:
                        continue
                    self.last_event_id = e["id"]
                    if e["kind"] != "message.new":
                        continue
                    payload = e["payload"]
                    msg = payload.get("message")
                    if not msg:
                        continue
                    cid = e["channel_id"]
                    ch = db.conn().execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
                    if not ch:
                        continue
                    author = msg.get("author", {})
                    nick = author.get("username", "huddle")
                    body = msg.get("body", "")
                    for s in list(self.sessions):
                        if not s.workspace or s.workspace["id"] != ch["workspace_id"]:
                            continue
                        if ch["name"] not in s.joined:
                            continue
                        if author.get("id") == (s.user["id"] if s.user else None):
                            # Don't echo back to the originating IRC user.
                            continue
                        for line in body.splitlines() or [""]:
                            s.send(f":{nick}!{nick}@huddle PRIVMSG #{ch['name']} :{line}")
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(0.5)


async def run_irc():
    db.init_db()
    gw = IrcGateway()
    gw.last_event_id = db.latest_event_id()

    async def handle(reader, writer):
        sess = IrcSession(reader, writer, gw)
        await sess.run()

    server = await asyncio.start_server(handle, IRC_HOST, IRC_PORT, reuse_address=True)
    fan = asyncio.create_task(gw.fan_out_loop())
    try:
        async with server:
            await server.serve_forever()
    finally:
        fan.cancel()
        try:
            await fan
        except Exception:
            pass
