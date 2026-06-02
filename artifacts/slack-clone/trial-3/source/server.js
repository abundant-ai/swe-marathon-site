const express = require('express');
const http = require('http');
const { WebSocketServer } = require('ws');
const path = require('path');
const crypto = require('crypto');
const url = require('url');
const { getDb, generateId, generateToken } = require('./lib/db');
const { hashPassword, verifyPassword, authMiddleware } = require('./lib/auth');
const BroadcastClient = require('./lib/broadcast');

const PORT = parseInt(process.argv[2]) || 8000;
const NODE_ID = PORT - 8000;

const app = express();
const server = http.createServer(app);

// Middleware
app.use(express.json({ limit: '50mb' }));
app.use(express.urlencoded({ extended: true }));

// Serve static frontend
app.use(express.static(path.join(__dirname, 'public'), { index: false }));

// Track connected WebSocket clients for each channel
const channelClients = new Map(); // channelId -> Set<ws>
const wsClients = new Set(); // all ws connections

function addWsClient(channelId, ws) {
  if (!channelClients.has(channelId)) {
    channelClients.set(channelId, new Set());
  }
  channelClients.get(channelId).add(ws);
}

function removeWsClient(channelId, ws) {
  if (channelClients.has(channelId)) {
    channelClients.get(channelId).delete(ws);
    if (channelClients.get(channelId).size === 0) {
      channelClients.delete(channelId);
    }
  }
}

function broadcastToChannel(channelId, event) {
  const data = JSON.stringify(event);
  if (channelClients.has(channelId)) {
    for (const ws of channelClients.get(channelId)) {
      if (ws.readyState === 1) {
        ws.send(data);
      }
    }
  }
}

// Broadcast client for cross-node communication
const broadcast = new BroadcastClient(PORT, (event) => {
  // Receive events from other nodes and deliver to local WebSocket clients
  if (event.channel_id) {
    broadcastToChannel(event.channel_id, event);
  }
});

// Internal endpoint to receive events from peer nodes (fallback)
// Dedup set for received events
const receivedEvents = new Map();
function isDuplicateEvent(event) {
  if (!event.channel_id || event.seq === undefined) return false;
  if (!receivedEvents.has(event.channel_id)) {
    receivedEvents.set(event.channel_id, new Set());
  }
  const channelSeqs = receivedEvents.get(event.channel_id);
  if (channelSeqs.has(event.seq)) return true;
  channelSeqs.add(event.seq);
  if (channelSeqs.size > 10000) {
    const arr = Array.from(channelSeqs);
    arr.sort((a,b) => a-b);
    for (let i = 0; i < arr.length - 5000; i++) {
      channelSeqs.delete(arr[i]);
    }
  }
  return false;
}

app.post('/__internal/event', (req, res) => {
  const event = req.body;
  if (event && event.channel_id && !isDuplicateEvent(event)) {
    broadcastToChannel(event.channel_id, event);
  }
  res.json({ ok: true });
});

// ============== HELPERS ==============

function getUserObj(user) {
  return {
    id: user.id,
    username: user.username,
    display_name: user.display_name || user.username,
    timezone: user.timezone || 'UTC',
    avatar_url: user.avatar_url || '',
    status_text: user.status_text || '',
    status_emoji: user.status_emoji || ''
  };
}

function getChannelObj(ch) {
  return {
    id: ch.id,
    workspace_id: ch.workspace_id,
    name: ch.name,
    is_private: !!ch.is_private,
    is_dm: !!ch.is_dm,
    topic: ch.topic || '',
    is_archived: !!ch.is_archived,
    created_at: ch.created_at
  };
}

function getMessageObj(msg) {
  const db = getDb();
  const files = db.prepare(`
    SELECT f.id, f.uploader_id, f.filename, f.content_type, f.size, f.created_at
    FROM files f JOIN message_files mf ON f.id = mf.file_id
    WHERE mf.message_id = ?
  `).all(msg.id);

  const reactions = db.prepare(`
    SELECT emoji, COUNT(*) as count, GROUP_CONCAT(user_id) as user_ids
    FROM message_reactions WHERE message_id = ?
    GROUP BY emoji
  `).all(msg.id).map(r => ({
    emoji: r.emoji,
    count: r.count,
    user_ids: r.user_ids ? r.user_ids.split(',') : []
  }));

  const author = db.prepare('SELECT * FROM users WHERE id = ?').get(msg.author_id);

  // Parse mentions from body
  const mentions = [];
  const mentionRegex = /@([a-zA-Z0-9_]+)/g;
  let match;
  const seen = new Set();
  while ((match = mentionRegex.exec(msg.body)) !== null) {
    const username = match[1];
    if (username === author?.username) continue;
    if (seen.has(username)) continue;
    seen.add(username);
    // Check if user exists and is member of the channel's workspace
    const channel = db.prepare('SELECT workspace_id FROM channels WHERE id = ?').get(msg.channel_id);
    if (channel) {
      const m = db.prepare(`
        SELECT u.id FROM users u
        JOIN workspace_members wm ON u.id = wm.user_id
        WHERE u.username = ? AND wm.workspace_id = ?
      `).get(username, channel.workspace_id);
      if (m) {
        mentions.push(m.id);
      }
    }
  }

  return {
    id: msg.id,
    channel_id: msg.channel_id,
    author_id: msg.author_id,
    author: author ? getUserObj(author) : null,
    body: msg.body,
    parent_id: msg.parent_id || null,
    reply_count: db.prepare('SELECT COUNT(*) as c FROM messages WHERE parent_id = ? AND deleted = 0').get(msg.id)?.c || 0,
    created_at: msg.created_at,
    edited_at: msg.edited_at || null,
    files: files || [],
    reactions: reactions || [],
    mentions: mentions,
    seq: msg.seq
  };
}

function requireWorkspaceRole(workspaceSlug, userId, allowedRoles) {
  const db = getDb();
  const ws = db.prepare('SELECT * FROM workspaces WHERE slug = ?').get(workspaceSlug);
  if (!ws) return { error: 'Not found', status: 404 };
  const member = db.prepare('SELECT * FROM workspace_members WHERE workspace_id = ? AND user_id = ?').get(ws.id, userId);
  if (!member) return { error: 'Not a member', status: 403 };
  if (!allowedRoles.includes(member.role)) return { error: 'Forbidden', status: 403 };
  return { ws, member };
}

// ============== API ROUTES ==============

// Health
app.get('/api/health', (req, res) => {
  res.json({ status: 'ok', node_id: NODE_ID });
});

// Auth routes
app.post('/api/auth/register', (req, res) => {
  const { username, password, display_name } = req.body;
  
  // Validate username (letters, digits, underscore, no spaces)
  if (!username || !/^[a-zA-Z0-9_]+$/.test(username)) {
    return res.status(400).json({ error: 'Invalid username. Use letters, digits, and underscores only.' });
  }
  if (!password || password.length < 8) {
    return res.status(400).json({ error: 'Password must be at least 8 characters long.' });
  }

  const db = getDb();
  
  // Checkduplicate
  const existing = db.prepare('SELECT id FROM users WHERE username = ?').get(username);
  if (existing) {
    return res.status(409).json({ error: 'Username already taken' });
  }

  const id = generateId();
  const pwHash = hashPassword(password);
  const now = new Date().toISOString();
  
  db.prepare('INSERT INTO users (id, username, password_hash, display_name, created_at) VALUES (?, ?, ?, ?, ?)')
    .run(id, username, pwHash, display_name || username, now);

  const token = generateToken();
  db.prepare('INSERT INTO tokens (token, user_id, created_at) VALUES (?, ?, ?)').run(token, id, now);

  const user = db.prepare('SELECT * FROM users WHERE id = ?').get(id);
  res.status(201).json({ user: getUserObj(user), token });
});

app.post('/api/auth/login', (req, res) => {
  const { username, password } = req.body;
  if (!username || !password) {
    return res.status(400).json({ error: 'Username and password required' });
  }

  const db = getDb();
  const user = db.prepare('SELECT * FROM users WHERE username = ?').get(username);
  if (!user) {
    return res.status(401).json({ error: 'Invalid credentials' });
  }
  if (!verifyPassword(password, user.password_hash)) {
    return res.status(401).json({ error: 'Invalid credentials' });
  }

  const token = generateToken();
  db.prepare('INSERT INTO tokens (token, user_id, created_at) VALUES (?, ?, ?)').run(token, user.id, new Date().toISOString());

  res.json({ user: getUserObj(user), token });
});

app.get('/api/auth/me', authMiddleware, (req, res) => {
  res.json({ user: getUserObj(req.user) });
});

// ========== USER PROFILES ==========

app.get('/api/users/by-username/:username', authMiddleware, (req, res) => {
  const db = getDb();
  const user = db.prepare('SELECT * FROM users WHERE username = ?').get(req.params.username);
  if (!user) return res.status(404).json({ error: 'User not found' });
  res.json({ user: getUserObj(user) });
});

app.get('/api/users/:id', authMiddleware, (req, res) => {
  const db = getDb();
  const user = db.prepare('SELECT * FROM users WHERE id = ?').get(req.params.id);
  if (!user) return res.status(404).json({ error: 'User not found' });
  res.json({ user: getUserObj(user) });
});

app.patch('/api/users/me', authMiddleware, (req, res) => {
  const db = getDb();
  const allowed = ['display_name', 'timezone', 'avatar_url', 'status_text', 'status_emoji'];
  const updates = [];
  const params = [];
  
  for (const key of allowed) {
    if (req.body[key] !== undefined) {
      // Validate timezone
      if (key === 'timezone' && req.body[key] && !/^[A-Za-z_\/\-]+$/.test(req.body[key])) {
        return res.status(400).json({ error: 'Invalid timezone' });
      }
      if (key === 'display_name' && req.body[key].length > 100) {
        return res.status(400).json({ error: 'Display name too long' });
      }
      updates.push(`${key} = ?`);
      params.push(req.body[key]);
    }
  }

  if (updates.length === 0) {
    const user = db.prepare('SELECT * FROM users WHERE id = ?').get(req.user.id);
    return res.json({ user: getUserObj(user) });
  }

  params.push(req.user.id);
  db.prepare(`UPDATE users SET ${updates.join(', ')} WHERE id = ?`).run(...params);

  const user = db.prepare('SELECT * FROM users WHERE id = ?').get(req.user.id);
  
  // Broadcast user update to all channels the user is in
  const channels = db.prepare(`
    SELECT cm.channel_id FROM channel_members cm
    JOIN channels c ON cm.channel_id = c.id
    WHERE cm.user_id = ?
  `).all(req.user.id);
  for (const ch of channels) {
    broadcast.broadcast({ type: 'user.updated', channel_id: ch.channel_id, user: getUserObj(user) });
  }

  res.json({ user: getUserObj(user) });
});

// ========== WORKSPACES ==========

app.post('/api/workspaces', authMiddleware, (req, res) => {
  const { slug, name } = req.body;
  
  if (!slug || !/^[a-z0-9-]{2,32}$/.test(slug)) {
    return res.status(400).json({ error: 'Invalid slug. Must be 2-32 characters, lowercase letters, digits, hyphens.' });
  }
  if (!name || name.trim().length === 0) {
    return res.status(400).json({ error: 'Name is required' });
  }

  const db = getDb();
  
  const existing = db.prepare('SELECT id FROM workspaces WHERE slug = ?').get(slug);
  if (existing) {
    return res.status(409).json({ error: 'Workspace slug already taken' });
  }

  const wsId = generateId();
  const chId = generateId();
  const now = new Date().toISOString();

  db.prepare('INSERT INTO workspaces (id, slug, name, owner_id, created_at) VALUES (?, ?, ?, ?, ?)')
    .run(wsId, slug, name.trim(), req.user.id, now);

  db.prepare('INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (?, ?, ?)')
    .run(wsId, req.user.id, 'owner');

  // Create #general channel
  db.prepare('INSERT INTO channels (id, workspace_id, name, created_at) VALUES (?, ?, ?, ?)')
    .run(chId, wsId, 'general', now);

  db.prepare('INSERT INTO channel_members (channel_id, user_id) VALUES (?, ?)')
    .run(chId, req.user.id);

  const ws = db.prepare('SELECT * FROM workspaces WHERE id = ?').get(wsId);
  const channel = db.prepare('SELECT * FROM channels WHERE id = ?').get(chId);

  res.status(201).json({
    workspace: {
      id: ws.id,
      slug: ws.slug,
      name: ws.name,
      owner_id: ws.owner_id,
      join_mode: ws.join_mode,
      created_at: ws.created_at
    },
    general_channel: getChannelObj(channel)
  });
});

app.get('/api/workspaces', authMiddleware, (req, res) => {
  const db = getDb();
  const workspaces = db.prepare(`
    SELECT w.* FROM workspaces w
    JOIN workspace_members wm ON w.id = wm.workspace_id
    WHERE wm.user_id = ?
  `).all(req.user.id).map(w => ({
    id: w.id,
    slug: w.slug,
    name: w.name,
    owner_id: w.owner_id,
    join_mode: w.join_mode,
    created_at: w.created_at
  }));
  res.json({ workspaces });
});

app.get('/api/workspaces/:slug', authMiddleware, (req, res) => {
  const db = getDb();
  const ws = db.prepare('SELECT * FROM workspaces WHERE slug = ?').get(req.params.slug);
  if (!ws) return res.status(404).json({ error: 'Workspace not found' });

  const member = db.prepare('SELECT * FROM workspace_members WHERE workspace_id = ? AND user_id = ?').get(ws.id, req.user.id);
  if (!member) return res.status(403).json({ error: 'Not a workspace member' });

  const includeArchived = req.query.include_archived === 'true';
  let channelsQuery;
  if (includeArchived) {
    channelsQuery = db.prepare(`
      SELECT c.* FROM channels c
      JOIN channel_members cm ON c.id = cm.channel_id
      WHERE c.workspace_id = ? AND cm.user_id = ? AND c.is_dm = 0 AND c.workspace_id != ''
      ORDER BY c.name
    `);
  } else {
    channelsQuery = db.prepare(`
      SELECT c.* FROM channels c
      JOIN channel_members cm ON c.id = cm.channel_id
      WHERE c.workspace_id = ? AND cm.user_id = ? AND c.is_archived = 0 AND c.is_dm = 0 AND c.workspace_id != ''
      ORDER BY c.name
    `);
  }
  const channels = channelsQuery.all(ws.id, req.user.id).map(c => getChannelObj(c));

  const members = db.prepare(`
    SELECT u.id, u.username, u.display_name, wm.role
    FROM workspace_members wm
    JOIN users u ON wm.user_id = u.id
    WHERE wm.workspace_id = ?
  `).all(ws.id);

  // Read state
  const readState = {};
  const rows = db.prepare('SELECT channel_id, last_read_seq FROM read_state WHERE user_id = ?').all(req.user.id);
  for (const r of rows) {
    readState[r.channel_id] = r.last_read_seq;
  }

  res.json({
    workspace: {
      id: ws.id,
      slug: ws.slug,
      name: ws.name,
      owner_id: ws.owner_id,
      join_mode: ws.join_mode,
      created_at: ws.created_at
    },
    channels,
    members: members.map(m => ({ user_id: m.id, role: m.role })),
    read_state: readState
  });
});

app.patch('/api/workspaces/:slug', authMiddleware, (req, res) => {
  const result = requireWorkspaceRole(req.params.slug, req.user.id, ['owner', 'admin']);
  if (result.error) return res.status(result.status).json({ error: result.error });

  const db = getDb();
  const updates = [];
  const params = [];

  if (req.body.name !== undefined) {
    updates.push('name = ?');
    params.push(req.body.name.trim());
  }
  if (req.body.join_mode !== undefined) {
    if (!['open', 'invite_only'].includes(req.body.join_mode)) {
      return res.status(400).json({ error: 'Invalid join_mode' });
    }
    updates.push('join_mode = ?');
    params.push(req.body.join_mode);
  }

  if (updates.length === 0) {
    return res.json({ workspace: { id: result.ws.id, slug: result.ws.slug, name: result.ws.name, owner_id: result.ws.owner_id, join_mode: result.ws.join_mode, created_at: result.ws.created_at } });
  }

  params.push(result.ws.id);
  db.prepare(`UPDATE workspaces SET ${updates.join(', ')} WHERE id = ?`).run(...params);

  const ws = db.prepare('SELECT * FROM workspaces WHERE id = ?').get(result.ws.id);
  res.json({ workspace: { id: ws.id, slug: ws.slug, name: ws.name, owner_id: ws.owner_id, join_mode: ws.join_mode, created_at: ws.created_at } });
});

app.get('/api/workspaces/:slug/members', authMiddleware, (req, res) => {
  const db = getDb();
  const ws = db.prepare('SELECT * FROM workspaces WHERE slug = ?').get(req.params.slug);
  if (!ws) return res.status(404).json({ error: 'Workspace not found' });

  const member = db.prepare('SELECT * FROM workspace_members WHERE workspace_id = ? AND user_id = ?').get(ws.id, req.user.id);
  if (!member) return res.status(403).json({ error: 'Not a workspace member' });

  const members = db.prepare(`
    SELECT u.id, u.username, u.display_name, wm.role
    FROM workspace_members wm
    JOIN users u ON wm.user_id = u.id
    WHERE wm.workspace_id = ?
  `).all(ws.id);

  res.json({ members: members.map(m => ({ user_id: m.id, role: m.role })) });
});

app.patch('/api/workspaces/:slug/members/:user_id', authMiddleware, (req, res) => {
  const db = getDb();
  const ws = db.prepare('SELECT * FROM workspaces WHERE slug = ?').get(req.params.slug);
  if (!ws) return res.status(404).json({ error: 'Workspace not found' });

  const caller = db.prepare('SELECT * FROM workspace_members WHERE workspace_id = ? AND user_id = ?').get(ws.id, req.user.id);
  if (!caller) return res.status(403).json({ error: 'Not a workspace member' });

  const target = db.prepare('SELECT * FROM workspace_members WHERE workspace_id = ? AND user_id = ?').get(ws.id, req.params.user_id);
  if (!target) return res.status(404).json({ error: 'Member not found' });

  const newRole = req.body.role;
  if (!newRole || !['owner', 'admin', 'member', 'guest'].includes(newRole)) {
    return res.status(400).json({ error: 'Invalid role' });
  }

  // Cannot set role to owner via PATCH
  if (newRole === 'owner') {
    return res.status(400).json({ error: 'Use transfer_ownership to change owner' });
  }

  // Cannot modify owner's role
  if (target.role === 'owner') {
    return res.status(403).json({ error: 'Cannot change owner role' });
  }

  // Admins cannot modify other admins
  if (caller.role === 'admin' && target.role === 'admin') {
    return res.status(403).json({ error: 'Admins cannot modify other admins' });
  }

  // Only owner or admin can change roles
  if (!['owner', 'admin'].includes(caller.role)) {
    return res.status(403).json({ error: 'Forbidden' });
  }

  db.prepare('UPDATE workspace_members SET role = ? WHERE workspace_id = ? AND user_id = ?').run(newRole, ws.id, req.params.user_id);

  res.json({ user_id: req.params.user_id, role: newRole });
});

app.post('/api/workspaces/:slug/transfer_ownership', authMiddleware, (req, res) => {
  const db = getDb();
  const ws = db.prepare('SELECT * FROM workspaces WHERE slug = ?').get(req.params.slug);
  if (!ws) return res.status(404).json({ error: 'Workspace not found' });

  const caller = db.prepare('SELECT * FROM workspace_members WHERE workspace_id = ? AND user_id = ?').get(ws.id, req.user.id);
  if (!caller || caller.role !== 'owner') {
    return res.status(403).json({ error: 'Only the owner can transfer ownership' });
  }

  const target = db.prepare('SELECT * FROM workspace_members WHERE workspace_id = ? AND user_id = ?').get(ws.id, req.body.user_id);
  if (!target) return res.status(404).json({ error: 'Target user not a member' });

  db.prepare('UPDATE workspace_members SET role = ? WHERE workspace_id = ? AND user_id = ?').run('admin', ws.id, req.user.id);
  db.prepare('UPDATE workspace_members SET role = ? WHERE workspace_id = ? AND user_id = ?').run('owner', ws.id, req.body.user_id);
  db.prepare('UPDATE workspaces SET owner_id = ? WHERE id = ?').run(req.body.user_id, ws.id);

  res.json({ workspace: { id: ws.id, slug: ws.slug, name: ws.name, owner_id: req.body.user_id, join_mode: ws.join_mode, created_at: ws.created_at } });
});

// ========== CHANNELS ==========

app.post('/api/workspaces/:slug/channels', authMiddleware, (req, res) => {
  const db = getDb();
  const ws = db.prepare('SELECT * FROM workspaces WHERE slug = ?').get(req.params.slug);
  if (!ws) return res.status(404).json({ error: 'Workspace not found' });

  const member = db.prepare('SELECT * FROM workspace_members WHERE workspace_id = ? AND user_id = ?').get(ws.id, req.user.id);
  if (!member) return res.status(403).json({ error: 'Not a workspace member' });

  // Guests cannot create channels
  if (member.role === 'guest') {
    return res.status(403).json({ error: 'Guests cannot create channels' });
  }

  const { name, is_private, topic } = req.body;
  
  if (!name || !/^[a-z0-9-]{2,32}$/.test(name)) {
    return res.status(400).json({ error: 'Invalid channel name. Must be 2-32 characters, lowercase letters, digits, hyphens.' });
  }

  // Regular members cannot create private channels
  if (is_private && !['admin', 'owner'].includes(member.role)) {
    return res.status(403).json({ error: 'Only admins and owners can create private channels' });
  }

  if (topic && topic.length > 250) {
    return res.status(400).json({ error: 'Topic too long (max 250 chars)' });
  }

  const existing = db.prepare('SELECT id FROM channels WHERE workspace_id = ? AND name = ?').get(ws.id, name);
  if (existing) {
    return res.status(409).json({ error: 'Channel name already exists in this workspace' });
  }

  const chId = generateId();
  const now = new Date().toISOString();

  db.prepare('INSERT INTO channels (id, workspace_id, name, is_private, topic, created_at) VALUES (?, ?, ?, ?, ?, ?)')
    .run(chId, ws.id, name, is_private ? 1 : 0, topic || '', now);

  // Creator joins
  db.prepare('INSERT OR IGNORE INTO channel_members (channel_id, user_id) VALUES (?, ?)').run(chId, req.user.id);

  const channel = db.prepare('SELECT * FROM channels WHERE id = ?').get(chId);
  res.status(201).json({ channel: getChannelObj(channel) });
});

app.post('/api/channels/:id/join', authMiddleware, (req, res) => {
  const db = getDb();
  const ch = db.prepare('SELECT * FROM channels WHERE id = ?').get(req.params.id);
  if (!ch) return res.status(404).json({ error: 'Channel not found' });

  const ws = db.prepare('SELECT * FROM workspaces WHERE id = ?').get(ch.workspace_id);
  if (!ws) return res.status(404).json({ error: 'Workspace not found' });

  let member = db.prepare('SELECT * FROM workspace_members WHERE workspace_id = ? AND user_id = ?').get(ws.id, req.user.id);
  
  // If not a member, handle open/invite_only
  if (!member) {
    if (ws.join_mode === 'invite_only') {
      return res.status(403).json({ error: 'Invite-only workspace. You must be invited.' });
    }
    if (ch.is_dm) {
      return res.status(403).json({ error: 'Cannot join DM directly' });
    }
    // Auto-onboard on open workspace for public channels
    if (!ch.is_private) {
      db.prepare('INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (?, ?, ?)')
        .run(ws.id, req.user.id, 'member');
      member = { role: 'member' };
    } else {
      return res.status(403).json({ error: 'Cannot join private channel without workspace membership' });
    }
  }

  // Guest cannot join public channels
  if (member.role === 'guest' && !ch.is_private && !ch.is_dm) {
    return res.status(403).json({ error: 'Guests cannot join public channels' });
  }

  // Private channels require invitation (join via invite)
  if (ch.is_private && !ch.is_dm) {
    // Check if user was invited (has an invitation that was accepted)
    // For now, allow if admin/owner, otherwise require existing membership
    const chMember = db.prepare('SELECT * FROM channel_members WHERE channel_id = ? AND user_id = ?').get(ch.id, req.user.id);
    if (!chMember && !['admin', 'owner'].includes(member.role)) {
      return res.status(403).json({ error: 'Cannot join private channel without an invitation' });
    }
  }

  db.prepare('INSERT OR IGNORE INTO channel_members (channel_id, user_id) VALUES (?, ?)').run(ch.id, req.user.id);

  // Broadcast member.joined
  const user = db.prepare('SELECT * FROM users WHERE id = ?').get(req.user.id);
  const seq = nextSeq(db, ch.id);
  const event = { type: 'member.joined', seq, channel_id: ch.id, user: getUserObj(user) };
  broadcast.broadcast(event);
  broadcastToChannel(ch.id, event);

  res.json({ ok: true });
});

app.delete('/api/channels/:id/members/me', authMiddleware, (req, res) => {
  const db = getDb();
  const ch = db.prepare('SELECT * FROM channels WHERE id = ?').get(req.params.id);
  if (!ch) return res.status(404).json({ error: 'Channel not found' });

  db.prepare('DELETE FROM channel_members WHERE channel_id = ? AND user_id = ?').run(ch.id, req.user.id);

  // Broadcast member.left
  const seq = nextSeq(db, ch.id);
  const event = { type: 'member.left', seq, channel_id: ch.id, user_id: req.user.id };
  broadcast.broadcast(event);
  broadcastToChannel(ch.id, event);

  res.status(204).end();
});

app.patch('/api/channels/:id', authMiddleware, (req, res) => {
  const db = getDb();
  const ch = db.prepare('SELECT * FROM channels WHERE id = ?').get(req.params.id);
  if (!ch) return res.status(404).json({ error: 'Channel not found' });

  const ws = db.prepare('SELECT * FROM workspaces WHERE id = ?').get(ch.workspace_id);
  const member = db.prepare('SELECT * FROM workspace_members WHERE workspace_id = ? AND user_id = ?').get(ws.id, req.user.id);
  if (!member || !['owner', 'admin'].includes(member.role)) {
    return res.status(403).json({ error: 'Forbidden' });
  }

  const updates = [];
  const params = [];

  if (req.body.topic !== undefined) {
    if (req.body.topic.length > 250) {
      return res.status(400).json({ error: 'Topic too long (max 250 chars)' });
    }
    updates.push('topic = ?');
    params.push(req.body.topic);
  }
  if (req.body.is_archived !== undefined) {
    updates.push('is_archived = ?');
    params.push(req.body.is_archived ? 1 : 0);
  }
  if (req.body.name !== undefined) {
    if (!/^[a-z0-9-]{2,32}$/.test(req.body.name)) {
      return res.status(400).json({ error: 'Invalid channel name' });
    }
    // Check duplicate
    const dup = db.prepare('SELECT id FROM channels WHERE workspace_id = ? AND name = ? AND id != ?').get(ws.id, req.body.name, ch.id);
    if (dup) return res.status(409).json({ error: 'Channel name already exists' });
    updates.push('name = ?');
    params.push(req.body.name);
  }

  if (updates.length === 0) {
    return res.json({ channel: getChannelObj(ch) });
  }

  params.push(ch.id);
  db.prepare(`UPDATE channels SET ${updates.join(', ')} WHERE id = ?`).run(...params);

  const updated = db.prepare('SELECT * FROM channels WHERE id = ?').get(ch.id);
  
  // Broadcast channel update
  const seq = nextSeq(db, ch.id);
  const event = { type: 'channel.updated', seq, channel_id: ch.id, channel: getChannelObj(updated) };
  broadcast.broadcast(event);
  broadcastToChannel(ch.id, event);

  res.json({ channel: getChannelObj(updated) });
});

// ========== PINS ==========

app.get('/api/channels/:id/pins', authMiddleware, (req, res) => {
  const db = getDb();
  const ch = db.prepare('SELECT * FROM channels WHERE id = ?').get(req.params.id);
  if (!ch) return res.status(404).json({ error: 'Channel not found' });

  // Check if user can access this channel
  const chMember = db.prepare('SELECT * FROM channel_members WHERE channel_id = ? AND user_id = ?').get(ch.id, req.user.id);
  if (!chMember && ch.is_private) {
    return res.status(403).json({ error: 'Forbidden' });
  }

  const pins = db.prepare(`
    SELECT m.* FROM messages m
    JOIN pinned_messages pm ON m.id = pm.message_id
    WHERE pm.channel_id = ? AND m.deleted = 0
    ORDER BY m.created_at DESC
  `).all(ch.id).map(m => getMessageObj(m));

  res.json({ pins });
});

app.post('/api/messages/:id/pin', authMiddleware, (req, res) => {
  const db = getDb();
  const msg = db.prepare('SELECT * FROM messages WHERE id = ? AND deleted = 0').get(req.params.id);
  if (!msg) return res.status(404).json({ error: 'Message not found' });

  const ch = db.prepare('SELECT * FROM channels WHERE id = ?').get(msg.channel_id);
  const chMember = db.prepare('SELECT * FROM channel_members WHERE channel_id = ? AND user_id = ?').get(ch.id, req.user.id);
  if (!chMember) return res.status(403).json({ error: 'Forbidden' });

  db.prepare('INSERT OR IGNORE INTO pinned_messages (channel_id, message_id) VALUES (?, ?)').run(ch.id, msg.id);
  res.status(201).json({ ok: true });
});

app.delete('/api/messages/:id/pin', authMiddleware, (req, res) => {
  const db = getDb();
  const msg = db.prepare('SELECT * FROM messages WHERE id = ? AND deleted = 0').get(req.params.id);
  if (!msg) return res.status(404).json({ error: 'Message not found' });

  db.prepare('DELETE FROM pinned_messages WHERE channel_id = ? AND message_id = ?').run(msg.channel_id, msg.id);
  res.status(204).end();
});

// ========== MESSAGES ==========

function nextSeq(db, channelId) {
  db.prepare('UPDATE channels SET seq_counter = seq_counter + 1 WHERE id = ?').run(channelId);
  const ch = db.prepare('SELECT seq_counter FROM channels WHERE id = ?').get(channelId);
  return ch.seq_counter;
}

app.post('/api/channels/:id/messages', authMiddleware, (req, res) => {
  const db = getDb();
  const ch = db.prepare('SELECT * FROM channels WHERE id = ?').get(req.params.id);
  if (!ch) return res.status(404).json({ error: 'Channel not found' });

  if (ch.is_archived) {
    return res.status(423).json({ error: 'Channel is archived' });
  }

  const chMember = db.prepare('SELECT * FROM channel_members WHERE channel_id = ? AND user_id = ?').get(ch.id, req.user.id);
  if (!chMember) return res.status(403).json({ error: 'Not a channel member' });

  let { body, parent_id, file_ids } = req.body;

  // Handle slash commands
  if (body && body.startsWith('/') && !body.startsWith('//')) {
    const parts = body.split(/\s+/);
    const cmd = parts[0];
    const rest = parts.slice(1).join(' ');

    if (body === '//literal') {
      body = body.slice(1);
    } else if (cmd === '/me') {
      // Keep literal
    } else if (cmd === '/shrug') {
      body = rest ? `${rest} ¯\\_(ツ)_/¯` : '¯\\_(ツ)_/¯';
    } else if (cmd === '/topic') {
      if (!rest) return res.status(400).json({ error: 'Usage: /topic <new topic>' });
      // Check admin/owner
      const ws = db.prepare('SELECT * FROM workspaces WHERE id = ?').get(ch.workspace_id);
      const member = db.prepare('SELECT * FROM workspace_members WHERE workspace_id = ? AND user_id = ?').get(ws.id, req.user.id);
      if (!member || !['owner', 'admin'].includes(member.role)) {
        return res.status(403).json({ error: 'Forbidden' });
      }
      db.prepare('UPDATE channels SET topic = ? WHERE id = ?').run(rest, ch.id);
      const seq = nextSeq(db, ch.id);
      const updated = db.prepare('SELECT * FROM channels WHERE id = ?').get(ch.id);
      const event = { type: 'channel.updated', seq, channel_id: ch.id, channel: getChannelObj(updated) };
      broadcast.broadcast(event);
      broadcastToChannel(ch.id, event);
      return res.json({ channel: getChannelObj(updated) });
    } else if (cmd === '/invite') {
      // /invite @username
      const username = rest.replace(/^@/, '');
      const targetUser = db.prepare('SELECT * FROM users WHERE username = ?').get(username);
      if (!targetUser) return res.status(400).json({ error: 'User not found' });
      db.prepare('INSERT OR IGNORE INTO channel_members (channel_id, user_id) VALUES (?, ?)').run(ch.id, targetUser.id);
      return res.json({ ok: true });
    } else if (cmd === '/archive') {
      const ws = db.prepare('SELECT * FROM workspaces WHERE id = ?').get(ch.workspace_id);
      const member = db.prepare('SELECT * FROM workspace_members WHERE workspace_id = ? AND user_id = ?').get(ws.id, req.user.id);
      if (!member || !['owner', 'admin'].includes(member.role)) return res.status(403).json({ error: 'Forbidden' });
      db.prepare('UPDATE channels SET is_archived = 1 WHERE id = ?').run(ch.id);
      return res.json({ ok: true });
    } else if (cmd === '/unarchive') {
      const ws = db.prepare('SELECT * FROM workspaces WHERE id = ?').get(ch.workspace_id);
      const member = db.prepare('SELECT * FROM workspace_members WHERE workspace_id = ? AND user_id = ?').get(ws.id, req.user.id);
      if (!member || !['owner', 'admin'].includes(member.role)) return res.status(403).json({ error: 'Forbidden' });
      db.prepare('UPDATE channels SET is_archived = 0 WHERE id = ?').run(ch.id);
      return res.json({ ok: true });
    } else {
      return res.status(400).json({ error: `Unknown command: ${cmd}` });
    }
  }

  // Validate non-empty body
  if (!body || body.trim().length === 0) {
    return res.status(400).json({ error: 'Message body cannot be empty' });
  }

  const msgId = generateId();
  const now = new Date().toISOString();
  const seq = nextSeq(db, ch.id);

  // Validate parent_id
  if (parent_id) {
    const parent = db.prepare('SELECT * FROM messages WHERE id = ? AND channel_id = ? AND deleted = 0').get(parent_id, ch.id);
    if (!parent) return res.status(404).json({ error: 'Parent message not found' });
  }

  // Validate file_ids
  if (file_ids && file_ids.length > 0) {
    for (const fileId of file_ids) {
      const file = db.prepare('SELECT * FROM files WHERE id = ?').get(fileId);
      if (!file) return res.status(400).json({ error: `File ${fileId} not found` });
      if (file.uploader_id !== req.user.id) return res.status(400).json({ error: `File ${fileId} is not yours` });
      const attached = db.prepare('SELECT * FROM message_files WHERE file_id = ?').get(fileId);
      if (attached) return res.status(400).json({ error: `File ${fileId} already attached` });
    }
  }

  db.prepare('INSERT INTO messages (id, channel_id, author_id, body, parent_id, created_at, seq) VALUES (?, ?, ?, ?, ?, ?, ?)')
    .run(msgId, ch.id, req.user.id, body, parent_id || null, now, seq);

  if (file_ids) {
    for (const fileId of file_ids) {
      db.prepare('INSERT OR IGNORE INTO message_files (message_id, file_id) VALUES (?, ?)').run(msgId, fileId);
    }
  }

  const msg = db.prepare('SELECT * FROM messages WHERE id = ?').get(msgId);
  const msgObj = getMessageObj(msg);

  // Broadcast event
  const eventType = parent_id ? 'message.reply' : 'message.new';
  const event = { type: eventType, seq, channel_id: ch.id, message: msgObj };
  broadcast.broadcast(event);
  broadcastToChannel(ch.id, event);

  res.status(201).json({ message: msgObj });
});

app.get('/api/channels/:id/messages', authMiddleware, (req, res) => {
  const db = getDb();
  const ch = db.prepare('SELECT * FROM channels WHERE id = ?').get(req.params.id);
  if (!ch) return res.status(404).json({ error: 'Channel not found' });

  const chMember = db.prepare('SELECT * FROM channel_members WHERE channel_id = ? AND user_id = ?').get(ch.id, req.user.id);
  if (!chMember) return res.status(403).json({ error: 'Not a channel member' });

  const limit = parseInt(req.query.limit) || 50;
  const before = req.query.before;

  let query;
  if (before) {
    const beforeMsg = db.prepare('SELECT created_at FROM messages WHERE id = ?').get(before);
    if (beforeMsg) {
      query = db.prepare('SELECT * FROM messages WHERE channel_id = ? AND created_at < ? AND parent_id IS NULL AND deleted = 0 ORDER BY created_at DESC LIMIT ?');
      const messages = query.all(ch.id, beforeMsg.created_at, limit).map(m => getMessageObj(m));
      return res.json({ messages });
    }
  }
  query = db.prepare('SELECT * FROM messages WHERE channel_id = ? AND parent_id IS NULL AND deleted = 0 ORDER BY created_at DESC LIMIT ?');
  const messages = query.all(ch.id, limit).map(m => getMessageObj(m));
  return res.json({ messages });
});

app.patch('/api/messages/:id', authMiddleware, (req, res) => {
  const db = getDb();
  const msg = db.prepare('SELECT * FROM messages WHERE id = ? AND deleted = 0').get(req.params.id);
  if (!msg) return res.status(404).json({ error: 'Message not found' });

  if (msg.author_id !== req.user.id) {
    return res.status(403).json({ error: 'Only the author can edit this message' });
  }

  if (!req.body.body || req.body.body.trim().length === 0) {
    return res.status(400).json({ error: 'Message body cannot be empty' });
  }

  const now = new Date().toISOString();
  db.prepare('UPDATE messages SET body = ?, edited_at = ? WHERE id = ?').run(req.body.body, now, msg.id);

  const updated = db.prepare('SELECT * FROM messages WHERE id = ?').get(msg.id);
  const msgObj = getMessageObj(updated);

  const event = { type: 'message.edited', seq: msg.seq, channel_id: msg.channel_id, message: msgObj };
  broadcast.broadcast(event);
  broadcastToChannel(msg.channel_id, event);

  res.json({ message: msgObj });
});

app.delete('/api/messages/:id', authMiddleware, (req, res) => {
  const db = getDb();
  const msg = db.prepare('SELECT * FROM messages WHERE id = ? AND deleted = 0').get(req.params.id);
  if (!msg) {
    // Idempotent: if already deleted, return 204
    return res.status(204).end();
  }

  // Author or workspace owner
  const ch = db.prepare('SELECT * FROM channels WHERE id = ?').get(msg.channel_id);
  const ws = db.prepare('SELECT * FROM workspaces WHERE id = ?').get(ch.workspace_id);
  if (msg.author_id !== req.user.id && ws.owner_id !== req.user.id) {
    return res.status(403).json({ error: 'Forbidden' });
  }

  db.prepare('UPDATE messages SET deleted = 1 WHERE id = ?').run(msg.id);

  const event = { type: 'message.deleted', seq: msg.seq, channel_id: msg.channel_id, message_id: msg.id };
  broadcast.broadcast(event);
  broadcastToChannel(msg.channel_id, event);

  res.status(204).end();
});

app.get('/api/messages/:parent_id/replies', authMiddleware, (req, res) => {
  const db = getDb();
  const parent = db.prepare('SELECT * FROM messages WHERE id = ? AND deleted = 0').get(req.params.parent_id);
  if (!parent) return res.status(404).json({ error: 'Message not found' });

  const ch = db.prepare('SELECT * FROM channels WHERE id = ?').get(parent.channel_id);
  const chMember = db.prepare('SELECT * FROM channel_members WHERE channel_id = ? AND user_id = ?').get(ch.id, req.user.id);
  if (!chMember) return res.status(403).json({ error: 'Not a channel member' });

  const replies = db.prepare('SELECT * FROM messages WHERE parent_id = ? AND deleted = 0 ORDER BY created_at ASC').all(parent.id).map(m => getMessageObj(m));

  res.json({ messages: replies });
});

// ========== REACTIONS ==========

app.post('/api/messages/:id/reactions', authMiddleware, (req, res) => {
  const db = getDb();
  const msg = db.prepare('SELECT * FROM messages WHERE id = ? AND deleted = 0').get(req.params.id);
  if (!msg) return res.status(404).json({ error: 'Message not found' });

  const { emoji } = req.body;
  if (!emoji) return res.status(400).json({ error: 'Emoji is required' });

  db.prepare('INSERT OR IGNORE INTO message_reactions (message_id, user_id, emoji) VALUES (?, ?, ?)').run(msg.id, req.user.id, emoji);

  const updated = db.prepare('SELECT * FROM messages WHERE id = ?').get(msg.id);
  const msgObj = getMessageObj(updated);

  const event = { type: 'reaction.added', seq: msg.seq, channel_id: msg.channel_id, message_id: msg.id, emoji, user_id: req.user.id };
  broadcast.broadcast(event);
  broadcastToChannel(msg.channel_id, event);

  res.json({ message: msgObj });
});

app.delete('/api/messages/:id/reactions', authMiddleware, (req, res) => {
  const db = getDb();
  const msg = db.prepare('SELECT * FROM messages WHERE id = ? AND deleted = 0').get(req.params.id);
  if (!msg) return res.status(404).json({ error: 'Message not found' });

  const { emoji } = req.body;
  if (!emoji) return res.status(400).json({ error: 'Emoji is required' });

  db.prepare('DELETE FROM message_reactions WHERE message_id = ? AND user_id = ? AND emoji = ?').run(msg.id, req.user.id, emoji);

  const event = { type: 'reaction.removed', seq: msg.seq, channel_id: msg.channel_id, message_id: msg.id, emoji, user_id: req.user.id };
  broadcast.broadcast(event);
  broadcastToChannel(msg.channel_id, event);

  res.status(204).end();
});

// ========== DMs ==========

app.post('/api/dms', authMiddleware, (req, res) => {
  const db = getDb();
  const recipientId = req.body.recipient_id;
  const recipient = db.prepare('SELECT * FROM users WHERE id = ?').get(recipientId);
  if (!recipient) return res.status(404).json({ error: 'User not found' });

  // Check if DM already exists
  const existing = db.prepare(`
    SELECT c.id FROM channels c
    JOIN channel_members cm1 ON c.id = cm1.channel_id AND cm1.user_id = ?
    JOIN channel_members cm2 ON c.id = cm2.channel_id AND cm2.user_id = ?
    WHERE c.is_dm = 1
  `).get(req.user.id, recipientId);

  if (existing) {
    const channel = db.prepare('SELECT * FROM channels WHERE id = ?').get(existing.id);
    return res.json({ channel: getChannelObj(channel) });
  }

  // Create a shared workspace for the DM (or use first user's workspace?)
  // DMs don't require workspace membership; they are standalone
  const dmId = generateId();
  const now = new Date().toISOString();

  // DM channel name is based on participant usernames
  const dmName = `dm-${req.user.username}-${recipient.username}`;

  db.prepare('INSERT INTO channels (id, workspace_id, name, is_dm, created_at) VALUES (?, ?, ?, 1, ?)')
    .run(dmId, '', dmName, now);

  db.prepare('INSERT INTO channel_members (channel_id, user_id) VALUES (?, ?)').run(dmId, req.user.id);
  db.prepare('INSERT INTO channel_members (channel_id, user_id) VALUES (?, ?)').run(dmId, recipientId);

  const channel = db.prepare('SELECT * FROM channels WHERE id = ?').get(dmId);
  res.status(201).json({ channel: getChannelObj(channel) });
});

// ========== FILES ==========

const multer = require('multer');
const upload = multer({ storage: multer.memoryStorage(), limits: { fileSize: 10 * 1024 * 1024 } });

app.post('/api/files', authMiddleware, upload.single('file'), (req, res) => {
  if (!req.file) return res.status(400).json({ error: 'No file provided' });

  const db = getDb();
  const id = generateId();
  const now = new Date().toISOString();

  db.prepare('INSERT INTO files (id, uploader_id, filename, content_type, size, data, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)')
    .run(id, req.user.id, req.file.originalname, req.file.mimetype, req.file.size, req.file.buffer, now);

  const file = db.prepare('SELECT id, uploader_id, filename, content_type, size, created_at FROM files WHERE id = ?').get(id);
  res.status(201).json({ file });
});

app.get('/api/files/:id', authMiddleware, (req, res) => {
  const db = getDb();
  const file = db.prepare('SELECT id, uploader_id, filename, content_type, size, created_at FROM files WHERE id = ?').get(req.params.id);
  if (!file) return res.status(404).json({ error: 'File not found' });
  res.json({ file });
});

app.get('/api/files/:id/download', authMiddleware, (req, res) => {
  const db = getDb();
  const file = db.prepare('SELECT * FROM files WHERE id = ?').get(req.params.id);
  if (!file) return res.status(404).json({ error: 'File not found' });

  res.setHeader('Content-Type', file.content_type);
  res.setHeader('Content-Disposition', `attachment; filename="${file.filename}"`);
  res.send(file.data);
});

// ========== SEARCH ==========

app.get('/api/search', authMiddleware, (req, res) => {
  const db = getDb();
  const q = req.query.q;
  const workspace = req.query.workspace;
  if (!q) return res.status(400).json({ error: 'Query parameter q is required' });

  let wsId = null;
  if (workspace) {
    const ws = db.prepare('SELECT * FROM workspaces WHERE slug = ?').get(workspace);
    if (!ws) return res.status(404).json({ error: 'Workspace not found' });
    const member = db.prepare('SELECT * FROM workspace_members WHERE workspace_id = ? AND user_id = ?').get(ws.id, req.user.id);
    if (!member) return res.status(403).json({ error: 'Not a workspace member' });
    wsId = ws.id;
  }

  let query;
  if (wsId) {
    query = db.prepare(`
      SELECT m.* FROM messages m
      JOIN channels c ON m.channel_id = c.id
      JOIN channel_members cm ON c.id = cm.channel_id AND cm.user_id = ?
      WHERE c.workspace_id = ? AND m.body LIKE ? AND m.deleted = 0
      ORDER BY m.created_at DESC LIMIT 50
    `);
    const messages = query.all(req.user.id, wsId, `%${q}%`).map(m => getMessageObj(m));
    res.json({ messages });
  } else {
    query = db.prepare(`
      SELECT m.* FROM messages m
      JOIN channels c ON m.channel_id = c.id
      JOIN channel_members cm ON c.id = cm.channel_id AND cm.user_id = ?
      WHERE m.body LIKE ? AND m.deleted = 0
      ORDER BY m.created_at DESC LIMIT 50
    `);
    const messages = query.all(req.user.id, `%${q}%`).map(m => getMessageObj(m));
    res.json({ messages });
  }
});

// ========== READ STATE ==========

app.post('/api/channels/:id/read', authMiddleware, (req, res) => {
  const db = getDb();
  const ch = db.prepare('SELECT * FROM channels WHERE id = ?').get(req.params.id);
  if (!ch) return res.status(404).json({ error: 'Channel not found' });

  const lastRead = parseInt(req.body.last_read_seq) || 0;

  const existing = db.prepare('SELECT * FROM read_state WHERE user_id = ? AND channel_id = ?').get(req.user.id, ch.id);
  if (existing) {
    if (lastRead > existing.last_read_seq) {
      db.prepare('UPDATE read_state SET last_read_seq = ? WHERE user_id = ? AND channel_id = ?').run(lastRead, req.user.id, ch.id);
    }
  } else {
    db.prepare('INSERT INTO read_state (user_id, channel_id, last_read_seq) VALUES (?, ?, ?)').run(req.user.id, ch.id, lastRead);
  }

  res.json({ ok: true });
});

// ========== USER GROUPS ==========

app.get('/api/workspaces/:slug/groups', authMiddleware, (req, res) => {
  const db = getDb();
  const ws = db.prepare('SELECT * FROM workspaces WHERE slug = ?').get(req.params.slug);
  if (!ws) return res.status(404).json({ error: 'Workspace not found' });
  const member = db.prepare('SELECT * FROM workspace_members WHERE workspace_id = ? AND user_id = ?').get(ws.id, req.user.id);
  if (!member) return res.status(403).json({ error: 'Not a workspace member' });

  const groups = db.prepare('SELECT * FROM groups WHERE workspace_id = ?').all(ws.id).map(g => {
    const members = db.prepare('SELECT user_id FROM group_members WHERE workspace_id = ? AND group_id = ?').all(ws.id, g.id).map(r => r.user_id);
    return { handle: g.handle, name: g.name, member_user_ids: members };
  });

  res.json({ groups });
});

app.post('/api/workspaces/:slug/groups', authMiddleware, (req, res) => {
  const result = requireWorkspaceRole(req.params.slug, req.user.id, ['owner', 'admin']);
  if (result.error) return res.status(result.status).json({ error: result.error });

  const db = getDb();
  const { handle, name, member_user_ids } = req.body;
  if (!handle || !/^[a-z0-9_]+$/.test(handle)) return res.status(400).json({ error: 'Invalid handle' });
  if (!name) return res.status(400).json({ error: 'Name is required' });

  const existing = db.prepare('SELECT id FROM groups WHERE workspace_id = ? AND handle = ?').get(result.ws.id, handle);
  if (existing) return res.status(409).json({ error: 'Group handle already exists' });

  const id = generateId();
  db.prepare('INSERT INTO groups (id, workspace_id, handle, name) VALUES (?, ?, ?, ?)').run(id, result.ws.id, handle, name);

  if (member_user_ids && Array.isArray(member_user_ids)) {
    for (const uid of member_user_ids) {
      db.prepare('INSERT OR IGNORE INTO group_members (workspace_id, group_id, user_id) VALUES (?, ?, ?)').run(result.ws.id, id, uid);
    }
  }

  const members = db.prepare('SELECT user_id FROM group_members WHERE workspace_id = ? AND group_id = ?').all(result.ws.id, id).map(r => r.user_id);
  res.status(201).json({ handle, name, member_user_ids: members });
});

app.get('/api/workspaces/:slug/groups/:handle', authMiddleware, (req, res) => {
  const db = getDb();
  const ws = db.prepare('SELECT * FROM workspaces WHERE slug = ?').get(req.params.slug);
  if (!ws) return res.status(404).json({ error: 'Workspace not found' });
  const group = db.prepare('SELECT * FROM groups WHERE workspace_id = ? AND handle = ?').get(ws.id, req.params.handle);
  if (!group) return res.status(404).json({ error: 'Group not found' });
  const members = db.prepare('SELECT user_id FROM group_members WHERE workspace_id = ? AND group_id = ?').all(ws.id, group.id).map(r => r.user_id);
  res.json({ handle: group.handle, name: group.name, member_user_ids: members });
});

app.patch('/api/workspaces/:slug/groups/:handle', authMiddleware, (req, res) => {
  const result = requireWorkspaceRole(req.params.slug, req.user.id, ['owner', 'admin']);
  if (result.error) return res.status(result.status).json({ error: result.error });

  const db = getDb();
  const group = db.prepare('SELECT * FROM groups WHERE workspace_id = ? AND handle = ?').get(result.ws.id, req.params.handle);
  if (!group) return res.status(404).json({ error: 'Group not found' });

  if (req.body.name) {
    db.prepare('UPDATE groups SET name = ? WHERE workspace_id = ? AND id = ?').run(req.body.name, result.ws.id, group.id);
  }
  if (req.body.member_user_ids && Array.isArray(req.body.member_user_ids)) {
    db.prepare('DELETE FROM group_members WHERE workspace_id = ? AND group_id = ?').run(result.ws.id, group.id);
    for (const uid of req.body.member_user_ids) {
      db.prepare('INSERT OR IGNORE INTO group_members (workspace_id, group_id, user_id) VALUES (?, ?, ?)').run(result.ws.id, group.id, uid);
    }
  }

  const members = db.prepare('SELECT user_id FROM group_members WHERE workspace_id = ? AND group_id = ?').all(result.ws.id, group.id).map(r => r.user_id);
  res.json({ handle: group.handle, name: group.name, member_user_ids: members });
});

app.delete('/api/workspaces/:slug/groups/:handle', authMiddleware, (req, res) => {
  const result = requireWorkspaceRole(req.params.slug, req.user.id, ['owner', 'admin']);
  if (result.error) return res.status(result.status).json({ error: result.error });

  const db = getDb();
  const group = db.prepare('SELECT * FROM groups WHERE workspace_id = ? AND handle = ?').get(result.ws.id, req.params.handle);
  if (!group) return res.status(404).json({ error: 'Group not found' });

  db.prepare('DELETE FROM group_members WHERE workspace_id = ? AND group_id = ?').run(result.ws.id, group.id);
  db.prepare('DELETE FROM groups WHERE workspace_id = ? AND id = ?').run(result.ws.id, group.id);

  res.status(204).end();
});

// ========== INVITATIONS ==========

app.post('/api/workspaces/:slug/invitations', authMiddleware, (req, res) => {
  const result = requireWorkspaceRole(req.params.slug, req.user.id, ['owner', 'admin']);
  if (result.error) return res.status(result.status).json({ error: result.error });

  const db = getDb();
  const { email, expires_in, max_uses, invited_username } = req.body;
  const code = crypto.randomBytes(16).toString('hex');

  let expiresAt = null;
  if (expires_in) {
    expiresAt = new Date(Date.now() + expires_in * 1000).toISOString();
  }

  db.prepare('INSERT INTO invitations (code, workspace_id, email, invited_username, created_by, expires_at, max_uses) VALUES (?, ?, ?, ?, ?, ?, ?)')
    .run(code, result.ws.id, email || null, invited_username || null, req.user.id, expiresAt, max_uses || 1);

  res.status(201).json({ invitation: { code, workspace_id: result.ws.id, email: email || null, invited_username: invited_username || null, created_by: req.user.id, created_at: new Date().toISOString(), expires_at: expiresAt, max_uses: max_uses || 1, use_count: 0 } });
});

app.post('/api/invitations/:code/accept', authMiddleware, (req, res) => {
  const db = getDb();
  const inv = db.prepare('SELECT * FROM invitations WHERE code = ?').get(req.params.code);
  if (!inv) return res.status(404).json({ error: 'Invitation not found' });

  if (inv.expires_at && new Date(inv.expires_at) < new Date()) {
    return res.status(400).json({ error: 'Invitation expired' });
  }
  if (inv.use_count >= inv.max_uses) {
    return res.status(400).json({ error: 'Invitation exhausted' });
  }

  // Add user to workspace
  db.prepare('INSERT OR IGNORE INTO workspace_members (workspace_id, user_id, role) VALUES (?, ?, ?)').run(inv.workspace_id, req.user.id, 'member');
  db.prepare('UPDATE invitations SET use_count = use_count + 1 WHERE code = ?').run(inv.code);

  // Add to general channel
  const general = db.prepare('SELECT * FROM channels WHERE workspace_id = ? AND name = ?').get(inv.workspace_id, 'general');
  if (general) {
    db.prepare('INSERT OR IGNORE INTO channel_members (channel_id, user_id) VALUES (?, ?)').run(general.id, req.user.id);
  }

  res.json({ ok: true });
});

// ========== CATCH-ALL FOR SPA ==========

app.get('/', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

// ========== WEB SOCKET ==========

const wss = new WebSocketServer({ server, path: '/api/ws' });

wss.on('connection', (ws, req) => {
  const params = new URLSearchParams(req.url.split('?')[1] || '');
  const token = params.get('token');
  if (!token) {
    ws.close(4001, 'Missing token');
    return;
  }

  const db = getDb();
  const tok = db.prepare('SELECT * FROM tokens WHERE token = ?').get(token);
  if (!tok) {
    ws.close(4001, 'Invalid token');
    return;
  }
  const user = db.prepare('SELECT * FROM users WHERE id = ?').get(tok.user_id);
  if (!user) {
    ws.close(4001, 'User not found');
    return;
  }

  ws.userId = user.id;
  ws.subscriptions = new Set();
  wsClients.add(ws);

  ws.on('message', (data) => {
    try {
      const msg = JSON.parse(data.toString());
      if (msg.type === 'subscribe' && msg.channel_id) {
        // Verify access
        const ch = db.prepare('SELECT * FROM channels WHERE id = ?').get(msg.channel_id);
        if (!ch) {
          ws.send(JSON.stringify({ type: 'error', error: 'Channel not found' }));
          return;
        }
        const chMember = db.prepare('SELECT * FROM channel_members WHERE channel_id = ? AND user_id = ?').get(ch.id, user.id);
        if (!chMember) {
          ws.send(JSON.stringify({ type: 'error', error: 'Not a channel member' }));
          return;
        }

        ws.subscriptions.add(msg.channel_id);
        addWsClient(msg.channel_id, ws);

        // Handle resume
        if (msg.type === 'resume' && msg.since_seq !== undefined) {
          const sinceSeq = parseInt(msg.since_seq) || 0;
          const headSeq = ch.seq_counter;
          
          if (sinceSeq > 0 && sinceSeq < headSeq) {
            // Check if we have messages since then
            const oldestRetained = db.prepare('SELECT MIN(seq) as min_seq FROM messages WHERE channel_id = ?').get(ch.id);
            if (oldestRetained && oldestRetained.min_seq && sinceSeq < oldestRetained.min_seq) {
              ws.send(JSON.stringify({ type: 'resume.gap', channel_id: ch.id, earliest_seq: oldestRetained.min_seq }));
            }

            // Send missed messages
            const missed = db.prepare('SELECT * FROM messages WHERE channel_id = ? AND seq > ? AND deleted = 0 ORDER BY seq ASC').all(ch.id, sinceSeq);
            for (const m of missed) {
              const msgObj = getMessageObj(m);
              ws.send(JSON.stringify({ type: msgObj.parent_id ? 'message.reply' : 'message.new', seq: m.seq, channel_id: ch.id, message: msgObj }));
            }
          }
        }

        ws.send(JSON.stringify({ type: 'subscribed', channel_id: msg.channel_id, head_seq: ch.seq_counter }));
      } else if (msg.type === 'resume' && msg.channel_id) {
        const ch = db.prepare('SELECT * FROM channels WHERE id = ?').get(msg.channel_id);
        if (!ch) {
          ws.send(JSON.stringify({ type: 'error', error: 'Channel not found' }));
          return;
        }
        const sinceSeq = parseInt(msg.since_seq) || 0;
        const headSeq = ch.seq_counter;

        if (sinceSeq > 0 && sinceSeq < headSeq) {
          const oldestRetained = db.prepare('SELECT MIN(seq) as min_seq FROM messages WHERE channel_id = ?').get(ch.id);
          if (oldestRetained && oldestRetained.min_seq && sinceSeq < oldestRetained.min_seq) {
            ws.send(JSON.stringify({ type: 'resume.gap', channel_id: ch.id, earliest_seq: oldestRetained.min_seq }));
          }

          const missed = db.prepare('SELECT * FROM messages WHERE channel_id = ? AND seq > ? AND deleted = 0 ORDER BY seq ASC').all(ch.id, sinceSeq);
          for (const m of missed) {
            const msgObj = getMessageObj(m);
            ws.send(JSON.stringify({ type: msgObj.parent_id ? 'message.reply' : 'message.new', seq: m.seq, channel_id: ch.id, message: msgObj }));
          }
        }

        ws.send(JSON.stringify({ type: 'resumed', channel_id: ch.id, head_seq: headSeq }));
      }
    } catch (e) {
      // ignore
    }
  });

  ws.on('close', () => {
    wsClients.delete(ws);
    for (const channelId of ws.subscriptions) {
      removeWsClient(channelId, ws);
    }
  });
});

server.listen(parseInt(process.env.PORT) || PORT, process.env.HUDDLE_HTTP_HOST || '127.0.0.1', () => {
  console.log(`[server] node ${NODE_ID} listening on http://127.0.0.1:${PORT}`);
});

module.exports = { app, server };
