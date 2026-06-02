const net = require('net');
const WebSocket = require('ws');
const http = require('http');
const { getDb, generateId } = require('./lib/db');

const HUB_PORT = 9000;
const IRC_PORT = 6667;

const db = getDb();

const clients = new Map(); // socket -> { nick, userId, channels: Set }
const channelClients = new Map(); // #channelName -> Set<socket>
const channelIdToName = new Map();
const channelNameToId = new Map();

function refreshChannelMaps() {
  channelIdToName.clear();
  channelNameToId.clear();
  const channels = db.prepare('SELECT id, name FROM channels').all();
  for (const ch of channels) {
    channelIdToName.set(ch.id, ch.name);
    channelNameToId.set(ch.name, ch.id);
  }
}
refreshChannelMaps();

// Dedup for incoming events
const seenEvents = new Map();
function isDuplicate(event) {
  if (!event.channel_id || event.seq === undefined) return false;
  if (!seenEvents.has(event.channel_id)) seenEvents.set(event.channel_id, new Set());
  const s = seenEvents.get(event.channel_id);
  if (s.has(event.seq)) return true;
  s.add(event.seq);
  if (s.size > 5000) { const arr = Array.from(s); arr.sort((a,b)=>a-b); for (let i=0;i<arr.length-2500;i++) s.delete(arr[i]); }
  return false;
}

// Hub connection
let hubWs = null;
let hubConnected = false;

function connectHub() {
  try {
    hubWs = new WebSocket(`ws://127.0.0.1:${HUB_PORT}`);
    hubWs.on('open', () => { hubConnected = true; console.log('[irc] connected to hub'); });
    hubWs.on('message', (data) => {
      try {
        const event = JSON.parse(data.toString());
        if (event.type === 'welcome') return;
        if (!isDuplicate(event)) handleEvent(event);
      } catch (e) {}
    });
    hubWs.on('close', () => { hubConnected = false; setTimeout(connectHub, 2000); });
    hubWs.on('error', () => { hubConnected = false; setTimeout(connectHub, 2000); });
  } catch (e) { setTimeout(connectHub, 2000); }
}
connectHub();

function handleEvent(event) {
  if (!event.channel_id) return;
  const channelName = channelIdToName.get(event.channel_id);
  if (!channelName) { refreshChannelMaps(); return; }
  const chName = `#${channelName}`;
  switch (event.type) {
    case 'message.new':
    case 'message.reply': {
      const msg = event.message;
      const author = msg.author;
      const nick = author ? author.username : 'unknown';
      relayToChannel(chName, `:${nick}!~${nick}@localhost PRIVMSG ${chName} :${msg.body}`);
      break;
    }
    case 'message.edited': {
      const nick = event.message?.author?.username || 'unknown';
      relayToChannel(chName, `:${nick}!~${nick}@localhost NOTICE ${chName} :(edited) ${event.message.body}`);
      break;
    }
    case 'member.joined': {
      if (event.user && event.user.username) {
        relayToChannel(chName, `:${event.user.username}!~${event.user.username}@localhost JOIN ${chName}`);
      }
      break;
    }
  }
}

function relayToChannel(channelName, line) {
  if (channelClients.has(channelName)) {
    for (const sock of channelClients.get(channelName)) {
      sock.write(line + '\r\n');
    }
  }
}

function broadcastEvent(event) {
  // Send to hub
  if (hubConnected && hubWs && hubWs.readyState === 1) {
    hubWs.send(JSON.stringify(event));
  }
  // Also send directly to HTTP nodes
  const data = JSON.stringify(event);
  for (const port of [8000, 8001, 8002]) {
    const req = http.request({
      hostname: '127.0.0.1', port,
      path: '/__internal/event', method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) },
      timeout: 1000
    }, () => {});
    req.on('error', () => {});
    req.write(data);
    req.end();
  }
}

function nextSeq(channelId) {
  db.prepare('UPDATE channels SET seq_counter = seq_counter + 1 WHERE id = ?').run(channelId);
  const ch = db.prepare('SELECT seq_counter FROM channels WHERE id = ?').get(channelId);
  return ch.seq_counter;
}

// HTTP endpoint for internal events
const httpServer = http.createServer((req, res) => {
  if (req.method === 'POST' && req.url === '/__internal/event') {
    let body = '';
    req.on('data', chunk => body += chunk);
    req.on('end', () => {
      try {
        const event = JSON.parse(body);
        if (!isDuplicate(event)) handleEvent(event);
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: true }));
      } catch (e) { res.writeHead(400); res.end('{}'); }
    });
  } else {
    res.writeHead(200);
    res.end('irc ok');
  }
});
httpServer.listen(6700, '127.0.0.1', () => {
  console.log('[irc] http on 127.0.0.1:6700');
});

// TCP IRC server
const server = net.createServer((socket) => {
  let regState = { passed: false, nicked: false, usered: false, nick: '', userName: 'guest', realName: 'Guest', userId: null };
  let buffer = '';

  socket.on('data', (data) => {
    buffer += data.toString();
    const lines = buffer.split('\r\n');
    buffer = lines.pop() || '';
    for (const line of lines) {
      handleLine(socket, line.trim());
    }
  });

  function handleLine(sock, line) {
    if (!line) return;
    console.log(`[irc] recv: ${line}`);

    const parts = [];
    let current = '';
    let trailing = false;
    for (let i = 0; i < line.length; i++) {
      const ch = line[i];
      if (ch === ' ' && !trailing) {
        if (current) { parts.push(current); current = ''; }
      } else if (ch === ':' && (i === 0 || parts.length > 0 && current === '')) {
        trailing = true;
      } else {
        current += ch;
      }
    }
    if (current) parts.push(current);
    if (parts.length === 0) return;

    const command = parts[0].toUpperCase();

    // Handle pre-registration commands
    if (command === 'PASS') {
      const token = parts[1];
      const tok = db.prepare('SELECT * FROM tokens WHERE token = ?').get(token);
      if (!tok) {
        sock.write(':localhost 464 * :Password incorrect\r\n');
        sock.end();
        return;
      }
      regState.passed = true;
      regState.userId = tok.user_id;
      const user = db.prepare('SELECT * FROM users WHERE id = ?').get(tok.user_id);
      if (user) {
        regState.nick = user.username;
        regState.userName = user.username;
        regState.realName = user.display_name || user.username;
      }
      return;
    }

    if (command === 'NICK') {
      if (!regState.passed) {
        sock.write(':localhost 451 * :You have not registered\r\n');
        return;
      }
      const newNick = parts[1];
      // Check collision
      let collision = false;
      for (const [cs, info] of clients) {
        if (cs !== sock && info.nick === newNick) { collision = true; break; }
      }
      if (collision) {
        sock.write(`:localhost 433 * ${newNick} :Nickname is already in use\r\n`);
        return;
      }
      // If already registered, this is a nick change
      if (clients.has(sock)) {
        const info = clients.get(sock);
        const oldNick = info.nick;
        info.nick = newNick;
        for (const ch of info.channels) {
          relayToChannel(ch, `:${oldNick}!~${info.nick}@localhost NICK :${newNick}`);
        }
        return;
      }
      regState.nicked = true;
      regState.nick = newNick;
      // Check if we have both NICK and USER
      if (regState.nicked && regState.usered) {
        completeRegistration(sock);
      }
      return;
    }

    if (command === 'USER') {
      if (!regState.passed) {
        sock.write(':localhost 451 * :You have not registered\r\n');
        return;
      }
      if (parts.length >= 5) {
        regState.userName = parts[1];
        regState.realName = parts.slice(4).join(' ').replace(/^:/, '');
      }
      regState.usered = true;
      // Check if we have both NICK and USER
      if (regState.nicked && regState.usered) {
        completeRegistration(sock);
      }
      return;
    }

    // Unregistered clients
    if (!clients.has(sock)) {
      if (command === 'CAP' || command === 'PING') {
        if (command === 'PING') sock.write(`:localhost PONG localhost :${parts[1] || ''}\r\n`);
        return;
      }
      sock.write(':localhost 451 * :You have not registered\r\n');
      return;
    }

    const clientInfo = clients.get(sock);
    const nick = clientInfo.nick;
    const userId = clientInfo.userId;

    switch (command) {
      case 'JOIN': {
        const chName = parts[1];
        if (!chName || !chName.startsWith('#')) {
          sock.write(':localhost 403 * :No such channel\r\n');
          return;
        }
        const channelName = chName.slice(1);
        refreshChannelMaps();
        const channelId = channelNameToId.get(channelName);
        if (!channelId) {
          sock.write(`:localhost 403 ${nick} ${chName} :No such channel\r\n`);
          return;
        }
        const ch = db.prepare('SELECT * FROM channels WHERE id = ?').get(channelId);
        if (!ch) {
          sock.write(`:localhost 403 ${nick} ${chName} :No such channel\r\n`);
          return;
        }
        // Check membership
        const chMember = db.prepare('SELECT * FROM channel_members WHERE channel_id = ? AND user_id = ?').get(channelId, userId);
        if (!chMember) {
          sock.write(`:localhost 403 ${nick} ${chName} :You are not a member of this channel\r\n`);
          return;
        }
        // Join channel
        if (!channelClients.has(chName)) channelClients.set(chName, new Set());
        channelClients.get(chName).add(sock);
        clientInfo.channels.add(chName);

        const prefix = `:${nick}!~${clientInfo.userName || nick}@localhost`;
        sock.write(`${prefix} JOIN ${chName}\r\n`);
        relayToChannel(chName, `${prefix} JOIN ${chName}`);

        if (ch.topic) {
          sock.write(`:localhost 332 ${nick} ${chName} :${ch.topic}\r\n`);
        } else {
          sock.write(`:localhost 331 ${nick} ${chName} :No topic is set\r\n`);
        }

        const namesInChannel = [];
        for (const [cs, info] of clients) {
          if (info.channels.has(chName)) namesInChannel.push(info.nick);
        }
        sock.write(`:localhost 353 ${nick} = ${chName} :${namesInChannel.join(' ')}\r\n`);
        sock.write(`:localhost 366 ${nick} ${chName} :End of /NAMES list\r\n`);
        break;
      }

      case 'PRIVMSG': {
        const target = parts[1];
        const body = parts.slice(2).join(' ').replace(/^:/, '');
        if (!target || !body) {
          sock.write(`:localhost 411 ${nick} :No recipient given (PRIVMSG)\r\n`);
          return;
        }
        if (target.startsWith('#')) {
          const channelName = target.slice(1);
          refreshChannelMaps();
          const channelId = channelNameToId.get(channelName);
          if (!channelId) {
            sock.write(`:localhost 403 ${nick} ${target} :No such channel\r\n`);
            return;
          }
          const ch = db.prepare('SELECT * FROM channels WHERE id = ?').get(channelId);
          if (!ch || ch.is_archived) {
            sock.write(`:localhost 404 ${nick} ${target} :Cannot send to archived channel\r\n`);
            return;
          }

          const msgId = generateId();
          const now = new Date().toISOString();
          const seq = nextSeq(channelId);
          db.prepare('INSERT INTO messages (id, channel_id, author_id, body, created_at, seq) VALUES (?, ?, ?, ?, ?, ?)')
            .run(msgId, channelId, userId, body, now, seq);

          const author = db.prepare('SELECT * FROM users WHERE id = ?').get(userId);
          broadcastEvent({
            type: 'message.new', seq, channel_id: channelId,
            message: {
              id: msgId, channel_id: channelId, author_id: userId,
              author: { id: author.id, username: author.username, display_name: author.display_name || author.username, timezone: author.timezone, avatar_url: author.avatar_url, status_text: author.status_text, status_emoji: author.status_emoji },
              body, parent_id: null, reply_count: 0, created_at: now, edited_at: null, files: [], reactions: [], mentions: [], seq
            }
          });

          const prefix = `:${nick}!~${clientInfo.userName || nick}@localhost`;
          const relayMsg = `${prefix} PRIVMSG ${target} :${body}`;
          if (channelClients.has(target)) {
            for (const cs of channelClients.get(target)) {
              cs.write(relayMsg + '\r\n');
            }
          }
        } else {
          // DM - relay to target if connected
          for (const [cs, info] of clients) {
            if (info.nick === target) {
              cs.write(`:${nick}!~${clientInfo.userName || nick}@localhost PRIVMSG ${target} :${body}\r\n`);
              break;
            }
          }
        }
        break;
      }

      case 'PING':
        sock.write(`:localhost PONG localhost :${parts[1] || ''}\r\n`);
        break;

      case 'PONG':
        break;

      case 'QUIT':
        sock.write(':localhost ERROR :Closing link\r\n');
        sock.end();
        break;

      case 'NAMES': {
        const chName = parts[1];
        if (chName && chName.startsWith('#')) {
          const namesInChannel = [];
          if (channelClients.has(chName)) {
            for (const cs of channelClients.get(chName)) {
              const info = clients.get(cs);
              if (info) namesInChannel.push(info.nick);
            }
          }
          sock.write(`:localhost 353 ${nick} = ${chName} :${namesInChannel.join(' ')}\r\n`);
          sock.write(`:localhost 366 ${nick} ${chName} :End of /NAMES list\r\n`);
        }
        break;
      }

      case 'WHO': {
        const chName = parts[1];
        if (chName && chName.startsWith('#')) {
          if (channelClients.has(chName)) {
            for (const cs of channelClients.get(chName)) {
              const info = clients.get(cs);
              if (info) sock.write(`:localhost 352 ${nick} ${chName} ~${info.nick} localhost localhost ${info.nick} H :0 ${info.nick}\r\n`);
            }
          }
          sock.write(`:localhost 315 ${nick} ${chName} :End of WHO list\r\n`);
        }
        break;
      }

      case 'LIST':
        refreshChannelMaps();
        for (const [chName, chId] of channelNameToId) {
          const ch = db.prepare('SELECT * FROM channels WHERE id = ?').get(chId);
          if (ch && !ch.is_private) {
            const count = channelClients.has(`#${chName}`) ? channelClients.get(`#${chName}`).size : 0;
            sock.write(`:localhost 322 ${nick} #${chName} ${count} :${ch.topic || ''}\r\n`);
          }
        }
        sock.write(`:localhost 323 ${nick} :End of LIST\r\n`);
        break;

      case 'TOPIC': {
        const chName = parts[1];
        const newTopic = parts.slice(2).join(' ').replace(/^:/, '');
        if (!chName || !chName.startsWith('#')) {
          sock.write(`:localhost 403 ${nick} :No such channel\r\n`);
          return;
        }
        const channelName = chName.slice(1);
        refreshChannelMaps();
        const channelId = channelNameToId.get(channelName);
        if (!channelId) {
          sock.write(`:localhost 403 ${nick} ${chName} :No such channel\r\n`);
          return;
        }
        if (newTopic) {
          const ch = db.prepare('SELECT * FROM channels WHERE id = ?').get(channelId);
          if (ch && ch.workspace_id) {
            const ws = db.prepare('SELECT * FROM workspaces WHERE id = ?').get(ch.workspace_id);
            const member = db.prepare('SELECT * FROM workspace_members WHERE workspace_id = ? AND user_id = ?').get(ws.id, userId);
            if (member && ['owner', 'admin'].includes(member.role)) {
              db.prepare('UPDATE channels SET topic = ? WHERE id = ?').run(newTopic, channelId);
              relayToChannel(chName, `:${nick}!~${clientInfo.userName || nick}@localhost TOPIC ${chName} :${newTopic}`);
            } else {
              sock.write(`:localhost 482 ${nick} ${chName} :You're not channel operator\r\n`);
            }
          }
        } else {
          const ch = db.prepare('SELECT * FROM channels WHERE id = ?').get(channelId);
          if (ch && ch.topic) {
            sock.write(`:localhost 332 ${nick} ${chName} :${ch.topic}\r\n`);
          } else {
            sock.write(`:localhost 331 ${nick} ${chName} :No topic is set\r\n`);
          }
        }
        break;
      }

      case 'MODE':
        sock.write(`:localhost 221 ${nick} +\r\n`);
        break;

      case 'CAP':
        break;

      default:
        sock.write(`:localhost 421 ${nick} ${command} :Unknown command\r\n`);
        break;
    }
  }

  function completeRegistration(sock) {
    const info = {
      nick: regState.nick,
      userId: regState.userId,
      userName: regState.userName,
      realName: regState.realName,
      channels: new Set()
    };
    clients.set(sock, info);
    const n = info.nick;
    sock.write(`:localhost 001 ${n} :Welcome to Huddle IRC Gateway, ${n}\r\n`);
    sock.write(`:localhost 002 ${n} :Your host is localhost, running version 1.0\r\n`);
    sock.write(`:localhost 003 ${n} :This server was created now\r\n`);
    sock.write(`:localhost 004 ${n} localhost 1.0 o o\r\n`);
    sock.write(`:localhost 005 ${n} CHANTYPES=# PREFIX=(o)@ MODES= CHANLIMIT=#: NICKLEN=16 TOPICLEN=250 :are supported by this server\r\n`);
  }

  socket.on('close', () => {
    const info = clients.get(socket);
    if (info) {
      for (const chName of info.channels) {
        if (channelClients.has(chName)) {
          channelClients.get(chName).delete(socket);
          if (channelClients.get(chName).size === 0) channelClients.delete(chName);
        }
        relayToChannel(chName, `:${info.nick}!~${info.userName || info.nick}@localhost PART ${chName} :`);
      }
      clients.delete(socket);
    }
  });

  socket.on('error', () => { socket.destroy(); });
});

server.listen(IRC_PORT, '0.0.0.0', () => {
  console.log(`[irc] listening on 0.0.0.0:${IRC_PORT}`);
});
