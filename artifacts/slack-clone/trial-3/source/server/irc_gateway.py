"""IRC gateway bridge.

  - PASS authenticates with a bearer token. The token's user is the IRC user.
  - JOIN #channel maps to a channel in any workspace where the user is a
    member; if the user is in multiple workspaces with the same channel
    name, we pick the most-recent join. To keep this deterministic we look
    for a workspace where this user is a member and the channel exists.
  - PRIVMSG #channel commits via the same `store.post_message` so the
    per-channel `seq` stays dense across HTTP and IRC.
  - Messages from web/HTTP fan out via the broadcast client to all IRC
    clients subscribed to that channel.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import time
from typing import Any

sys.path.insert(0, "/app")
from server import store
from server.broadcast import BroadcastClient

HOST = os.environ.get("HUDDLE_IRC_HOST", "0.0.0.0")
PORT = int(os.environ.get("HUDDLE_IRC_PORT", "6667"))
SERVER_NAME = "huddle.local"

# active connections by nick
_clients: dict[str, "IRCClient"] = {}
_client_lock = asyncio.Lock()


class IRCClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.peer = writer.get_extra_info("peername") or ("?", 0)
        self.host = self.peer[0] if self.peer else "?"
        self.password: str | None = None
        self.user_id: str | None = None
        self.username: str | None = None
        self.nick: str | None = None
        self.userarg: str | None = None
        self.realname: str | None = None
        self.registered = False
        self.joined_channels: dict[str, str] = {}  # "#name" -> channel_id
        self.alive = True

    async def send_line(self, line: str) -> None:
        try:
            self.writer.write((line + "\r\n").encode("utf-8", errors="replace"))
            await self.writer.drain()
        except Exception:
            self.alive = False

    async def numeric(self, code: str, *args: str, trailing: str | None = None) -> None:
        nick = self.nick or "*"
        parts = [f":{SERVER_NAME}", code, nick, *args]
        line = " ".join(parts)
        if trailing is not None:
            line += f" :{trailing}"
        await self.send_line(line)

    def prefix(self) -> str:
        u = self.username or "user"
        return f"{self.nick}!{u}@{self.host}"

    async def close(self) -> None:
        self.alive = False
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass


def _parse_line(line: str) -> tuple[str, list[str]]:
    line = line.strip()
    if not line:
        return "", []
    parts: list[str] = []
    if " :" in line:
        head, _, trailing = line.partition(" :")
        parts = head.split()
        parts.append(trailing)
    else:
        parts = line.split()
    if not parts:
        return "", []
    cmd = parts[0].upper()
    return cmd, parts[1:]


async def _send_welcome(c: IRCClient) -> None:
    await c.numeric("001", trailing=f"Welcome to the Huddle IRC gateway {c.prefix()}")
    await c.numeric("002", trailing=f"Your host is {SERVER_NAME}, running version 1.0")
    await c.numeric("003", trailing="This server was created today")
    await c.numeric("004", SERVER_NAME, "1.0", "i", "ntm")
    await c.numeric("005", "PREFIX=(o)@", "CHANTYPES=#", "NETWORK=Huddle", trailing="are supported by this server")


async def _resolve_channel(c: IRCClient, name: str) -> str | None:
    """Map #name to a channel_id the user can access."""
    if not name.startswith("#"):
        return None
    short = name[1:]
    cur = store.conn()
    rows = cur.execute(
        "SELECT c.id FROM channels c JOIN workspace_members wm ON wm.workspace_id=c.workspace_id "
        "WHERE c.name=? AND wm.user_id=? AND c.is_dm=0 ORDER BY c.created_at LIMIT 1",
        (short, c.user_id),
    ).fetchall()
    if not rows:
        return None
    return rows[0]["id"]


async def _join_channel(c: IRCClient, name: str) -> None:
    chid = await _resolve_channel(c, name)
    if not chid:
        await c.numeric("403", name, trailing="No such channel")
        return
    ch_row = store.get_channel_row(chid)
    if not ch_row:
        await c.numeric("403", name, trailing="No such channel")
        return
    # ensure membership for private channels
    if ch_row["is_private"] and not store.is_channel_member(chid, c.user_id):
        await c.numeric("403", name, trailing="No such channel")
        return
    if not ch_row["is_private"]:
        with store.Tx():
            added = store.add_channel_member(chid, c.user_id)
            if added:
                u = store.get_user_by_id(c.user_id)
                seq, payload = store.commit_event(chid, "member.joined", {"user_id": c.user_id, "user": u})
            else:
                payload = None
        if payload:
            await BUS.dispatch(payload)
    c.joined_channels[name] = chid
    await c.send_line(f":{c.prefix()} JOIN {name}")
    topic = ch_row["topic"]
    if topic:
        await c.numeric("332", name, trailing=topic)
    else:
        await c.numeric("331", name, trailing="No topic is set")
    nicks = []
    for m in store.list_channel_members(chid):
        nicks.append(m["username"])
    await c.numeric("353", "=", name, trailing=" ".join(nicks))
    await c.numeric("366", name, trailing="End of /NAMES list")


async def _privmsg_channel(c: IRCClient, name: str, body: str) -> None:
    chid = c.joined_channels.get(name) or await _resolve_channel(c, name)
    if not chid:
        await c.numeric("403", name, trailing="No such channel")
        return
    ch = store.get_channel(chid)
    if not ch:
        await c.numeric("403", name, trailing="No such channel")
        return
    if ch["is_archived"]:
        await c.numeric("404", name, trailing="Cannot send to channel (archived)")
        return
    # ensure user is a member to write
    if (ch["is_private"] or ch["is_dm"]) and not store.is_channel_member(chid, c.user_id):
        await c.numeric("404", name, trailing="Cannot send to channel")
        return
    if not store.workspace_role(ch["workspace_id"], c.user_id):
        await c.numeric("404", name, trailing="Cannot send to channel")
        return
    if not body.strip():
        return
    with store.Tx():
        msg, payload = store.post_message(chid, c.user_id, body)
    await BUS.dispatch(payload)


async def _privmsg_user(c: IRCClient, target_nick: str, body: str) -> None:
    target = store.get_user_by_username(target_nick)
    if not target:
        await c.numeric("401", target_nick, trailing="No such nick/channel")
        return
    try:
        with store.Tx():
            ch, _created = store.get_or_create_dm(c.user_id, target["id"])
            msg, payload = store.post_message(ch["id"], c.user_id, body)
    except ValueError:
        await c.numeric("401", target_nick, trailing="No shared workspace")
        return
    await BUS.dispatch(payload)


async def _names(c: IRCClient, name: str) -> None:
    chid = await _resolve_channel(c, name)
    if not chid:
        await c.numeric("366", name, trailing="End of /NAMES list")
        return
    nicks = [m["username"] for m in store.list_channel_members(chid)]
    await c.numeric("353", "=", name, trailing=" ".join(nicks))
    await c.numeric("366", name, trailing="End of /NAMES list")


async def _list_channels(c: IRCClient) -> None:
    await c.numeric("321", "Channel", trailing="Users  Name")
    cur = store.conn()
    rows = cur.execute(
        "SELECT c.id, c.name, c.topic FROM channels c JOIN workspace_members wm ON wm.workspace_id=c.workspace_id "
        "WHERE wm.user_id=? AND c.is_dm=0 AND c.is_archived=0",
        (c.user_id,),
    ).fetchall()
    for r in rows:
        n = len(store.list_channel_members(r["id"]))
        await c.numeric("322", f"#{r['name']}", str(n), trailing=r["topic"] or "")
    await c.numeric("323", trailing="End of /LIST")


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    c = IRCClient(reader, writer)
    try:
        while c.alive:
            try:
                raw = await asyncio.wait_for(reader.readline(), timeout=300)
            except asyncio.TimeoutError:
                break
            if not raw:
                break
            try:
                line = raw.decode("utf-8", errors="replace")
            except Exception:
                continue
            cmd, args = _parse_line(line)
            if not cmd:
                continue
            if cmd == "PASS":
                if not args:
                    await c.numeric("461", "PASS", trailing="Not enough parameters")
                    continue
                token = args[0]
                user = store.user_for_token(token)
                if not user:
                    await c.numeric("464", trailing="Password incorrect")
                    await c.send_line("ERROR :Closing Link")
                    break
                c.password = token
                c.user_id = user["id"]
                c.username = user["username"]
                continue
            if cmd == "NICK":
                if not args:
                    await c.numeric("431", trailing="No nickname given")
                    continue
                requested = args[0]
                # Must have authenticated with PASS first
                if not c.user_id:
                    await c.numeric("464", trailing="Password required first")
                    await c.send_line("ERROR :Closing Link: PASS required")
                    break
                # nick must match the authenticated username, OR be unique
                if requested != c.username:
                    # collision check
                    if requested in _clients:
                        await c.numeric("433", requested, trailing="Nickname is already in use")
                        continue
                    # only allow registering nick == username; otherwise treat as collision-ish
                    await c.numeric("433", requested, trailing="Nickname must match account username")
                    continue
                async with _client_lock:
                    if requested in _clients and _clients[requested] is not c:
                        await c.numeric("433", requested, trailing="Nickname is already in use")
                        continue
                    c.nick = requested
                    _clients[requested] = c
                if c.userarg and not c.registered:
                    c.registered = True
                    await _send_welcome(c)
                continue
            if cmd == "USER":
                if len(args) < 4:
                    await c.numeric("461", "USER", trailing="Not enough parameters")
                    continue
                c.userarg = args[0]
                c.realname = args[3]
                if c.nick and not c.registered:
                    c.registered = True
                    await _send_welcome(c)
                continue
            if not c.registered:
                # quietly ignore until registered, except for PING
                if cmd == "PING":
                    payload = args[0] if args else SERVER_NAME
                    await c.send_line(f"PONG {SERVER_NAME} :{payload}")
                continue
            # ---- registered commands
            if cmd == "PING":
                payload = args[0] if args else SERVER_NAME
                await c.send_line(f"PONG {SERVER_NAME} :{payload}")
            elif cmd == "PONG":
                pass
            elif cmd == "QUIT":
                reason = args[0] if args else "Client Quit"
                await c.send_line(f"ERROR :Closing Link: {reason}")
                break
            elif cmd == "JOIN":
                if not args:
                    await c.numeric("461", "JOIN", trailing="Not enough parameters")
                    continue
                for chan in args[0].split(","):
                    await _join_channel(c, chan)
            elif cmd == "PART":
                if not args:
                    continue
                for chan in args[0].split(","):
                    chid = c.joined_channels.pop(chan, None)
                    if chid:
                        await c.send_line(f":{c.prefix()} PART {chan}")
            elif cmd == "PRIVMSG":
                if len(args) < 2:
                    await c.numeric("461", "PRIVMSG", trailing="Not enough parameters")
                    continue
                target = args[0]
                body = args[1]
                if target.startswith("#"):
                    await _privmsg_channel(c, target, body)
                else:
                    await _privmsg_user(c, target, body)
            elif cmd == "NAMES":
                if args:
                    for ch in args[0].split(","):
                        await _names(c, ch)
                else:
                    for ch in list(c.joined_channels):
                        await _names(c, ch)
            elif cmd == "LIST":
                await _list_channels(c)
            elif cmd == "WHO":
                if args:
                    target = args[0]
                    if target.startswith("#"):
                        chid = c.joined_channels.get(target) or await _resolve_channel(c, target)
                        if chid:
                            for m in store.list_channel_members(chid):
                                await c.numeric("352", target, m["username"], "huddle.local",
                                                SERVER_NAME, m["username"], "H",
                                                trailing=f"0 {m['display_name']}")
                    await c.numeric("315", args[0], trailing="End of /WHO list")
                else:
                    await c.numeric("315", "*", trailing="End of /WHO list")
            elif cmd == "TOPIC":
                if not args:
                    await c.numeric("461", "TOPIC", trailing="Not enough parameters")
                    continue
                chan = args[0]
                chid = c.joined_channels.get(chan) or await _resolve_channel(c, chan)
                if not chid:
                    await c.numeric("403", chan, trailing="No such channel")
                    continue
                if len(args) >= 2:
                    new_topic = args[1]
                    role = store.workspace_role(store.get_channel(chid)["workspace_id"], c.user_id)
                    if role not in ("owner", "admin"):
                        await c.numeric("482", chan, trailing="You are not channel operator")
                        continue
                    with store.Tx():
                        ch2 = store.update_channel(chid, {"topic": new_topic})
                        seq, payload = store.commit_event(chid, "channel.updated", {"channel": ch2})
                    await BUS.dispatch(payload)
                    await c.send_line(f":{c.prefix()} TOPIC {chan} :{new_topic}")
                else:
                    ch2 = store.get_channel(chid)
                    if ch2["topic"]:
                        await c.numeric("332", chan, trailing=ch2["topic"])
                    else:
                        await c.numeric("331", chan, trailing="No topic is set")
            elif cmd == "MODE":
                # acknowledge but mostly ignore; reply with empty mode
                if args:
                    target = args[0]
                    if target.startswith("#"):
                        await c.numeric("324", target, "+nt")
                    else:
                        await c.numeric("221", "+i")
            elif cmd == "CAP":
                # ignore
                if args and args[0].upper() == "LS":
                    await c.send_line(f":{SERVER_NAME} CAP * LS :")
                elif args and args[0].upper() == "END":
                    pass
            else:
                await c.numeric("421", cmd, trailing="Unknown command")
    except Exception:
        pass
    finally:
        if c.nick:
            async with _client_lock:
                if _clients.get(c.nick) is c:
                    del _clients[c.nick]
        await c.close()


# ---- bridging events from broadcast into IRC --------------------------------

class IRCBus:
    """Receives durable events and fans them out to IRC clients subscribed
    to the corresponding channel."""

    async def dispatch(self, payload: dict) -> None:
        kind = payload.get("type")
        chid = payload.get("channel_id")
        if not chid:
            return
        if kind in ("message.new",):
            msg = payload.get("message") or {}
            author_id = msg.get("author_id")
            body = msg.get("body", "")
            ch = store.get_channel(chid)
            if not ch:
                return
            chan_name = "#" + ch["name"]
            author = msg.get("author") or {}
            nick = author.get("username") or "?"
            host = "huddle.local"
            for line in body.splitlines() or [""]:
                line_str = f":{nick}!{nick}@{host} PRIVMSG {chan_name} :{line}"
                for c in list(_clients.values()):
                    if not c.registered or not c.alive:
                        continue
                    if author_id == c.user_id:
                        continue
                    if chan_name not in c.joined_channels:
                        continue
                    await c.send_line(line_str)


BUS = IRCBus()


async def main() -> None:
    store.init_db()
    bc = BroadcastClient(node_id=99)  # IRC gateway uses node_id 99 as a marker

    async def relay_in(payload: dict):
        await BUS.dispatch(payload)

    bc.add_listener(relay_in)
    await bc.start()

    server = await asyncio.start_server(handle_client, host=HOST, port=PORT, reuse_address=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
