const { DatabaseSync } = require('node:sqlite');
const path = require('path');
const crypto = require('crypto');

const DB_PATH = path.join(__dirname, '..', 'data', 'huddle.db');

let db;

function getDb() {
  if (!db) {
    db = new DatabaseSync(DB_PATH);
    db.exec('PRAGMA journal_mode=WAL');
    db.exec('PRAGMA busy_timeout=5000');
    db.exec('PRAGMA foreign_keys=ON');
    db.exec(`
      CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT '',
        timezone TEXT NOT NULL DEFAULT 'UTC',
        avatar_url TEXT NOT NULL DEFAULT '',
        status_text TEXT NOT NULL DEFAULT '',
        status_emoji TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
      )
    `);
    db.exec(`
      CREATE TABLE IF NOT EXISTS tokens (
        token TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
      )
    `);
    db.exec(`
      CREATE TABLE IF NOT EXISTS workspaces (
        id TEXT PRIMARY KEY,
        slug TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        owner_id TEXT NOT NULL REFERENCES users(id),
        join_mode TEXT NOT NULL DEFAULT 'open' CHECK (join_mode IN ('open','invite_only')),
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
      )
    `);
    db.exec(`
      CREATE TABLE IF NOT EXISTS workspace_members (
        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        user_id TEXT NOT NULL REFERENCES users(id),
        role TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('owner','admin','member','guest')),
        PRIMARY KEY (workspace_id, user_id)
      )
    `);
// channels: workspace_id can be empty for DMs (no FK)
    db.exec(`
      CREATE TABLE IF NOT EXISTS channels (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL,
        is_private INTEGER NOT NULL DEFAULT 0,
        is_dm INTEGER NOT NULL DEFAULT 0,
        topic TEXT NOT NULL DEFAULT '',
        is_archived INTEGER NOT NULL DEFAULT 0,
        seq_counter INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
      )
    `);
    db.exec(`
      CREATE TABLE IF NOT EXISTS channel_members (
        channel_id TEXT NOT NULL REFERENCES channels(id),
        user_id TEXT NOT NULL REFERENCES users(id),
        PRIMARY KEY (channel_id, user_id)
      )
    `);
    db.exec(`
      CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY,
        channel_id TEXT NOT NULL REFERENCES channels(id),
        author_id TEXT NOT NULL REFERENCES users(id),
        body TEXT NOT NULL,
        parent_id TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        edited_at TEXT,
        seq INTEGER NOT NULL,
        deleted INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY (parent_id) REFERENCES messages(id)
      )
    `);
    db.exec(`
      CREATE TABLE IF NOT EXISTS message_reactions (
        message_id TEXT NOT NULL REFERENCES messages(id),
        user_id TEXT NOT NULL REFERENCES users(id),
        emoji TEXT NOT NULL,
        PRIMARY KEY (message_id, user_id, emoji)
      )
    `);
    db.exec(`
      CREATE TABLE IF NOT EXISTS pinned_messages (
        channel_id TEXT NOT NULL REFERENCES channels(id),
        message_id TEXT NOT NULL REFERENCES messages(id),
        PRIMARY KEY (channel_id, message_id)
      )
    `);
    db.exec(`
      CREATE TABLE IF NOT EXISTS read_state (
        user_id TEXT NOT NULL REFERENCES users(id),
        channel_id TEXT NOT NULL REFERENCES channels(id),
        last_read_seq INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, channel_id)
      )
    `);
    db.exec(`
      CREATE TABLE IF NOT EXISTS files (
        id TEXT PRIMARY KEY,
        uploader_id TEXT NOT NULL REFERENCES users(id),
        filename TEXT NOT NULL,
        content_type TEXT NOT NULL,
        size INTEGER NOT NULL,
        data BLOB NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
      )
    `);
    db.exec(`
      CREATE TABLE IF NOT EXISTS message_files (
        message_id TEXT NOT NULL REFERENCES messages(id),
        file_id TEXT NOT NULL REFERENCES files(id),
        PRIMARY KEY (message_id, file_id)
      )
    `);
    db.exec(`
      CREATE TABLE IF NOT EXISTS groups (
        id TEXT NOT NULL,
        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        handle TEXT NOT NULL,
        name TEXT NOT NULL,
        PRIMARY KEY (workspace_id, id)
      )
    `);
    db.exec(`
      CREATE TABLE IF NOT EXISTS group_members (
        workspace_id TEXT NOT NULL,
        group_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        PRIMARY KEY (workspace_id, group_id, user_id),
        FOREIGN KEY (workspace_id, group_id) REFERENCES groups(workspace_id, id)
      )
    `);
    db.exec(`
      CREATE TABLE IF NOT EXISTS invitations (
        code TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        email TEXT,
        invited_username TEXT,
        created_by TEXT NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        expires_at TEXT,
        max_uses INTEGER NOT NULL DEFAULT 1,
        use_count INTEGER NOT NULL DEFAULT 0
      )
    `);
    db.exec('CREATE INDEX IF NOT EXISTS idx_messages_channel_seq ON messages(channel_id, seq)');
    db.exec('CREATE INDEX IF NOT EXISTS idx_messages_parent ON messages(parent_id)');
    db.exec('CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(channel_id, created_at)');
    db.exec('CREATE INDEX IF NOT EXISTS idx_tokens_token ON tokens(token)');
    db.exec('CREATE INDEX IF NOT EXISTS idx_tokens_user ON tokens(user_id)');
    db.exec('CREATE INDEX IF NOT EXISTS idx_workspace_members_user ON workspace_members(user_id)');
    db.exec('CREATE INDEX IF NOT EXISTS idx_channel_members_user ON channel_members(user_id)');
    db.exec('CREATE INDEX IF NOT EXISTS idx_files_uploader ON files(uploader_id)');
    db.exec('CREATE INDEX IF NOT EXISTS idx_channels_workspace_name ON channels(workspace_id, name)');
  }
  return db;
}

function generateId() {
  return crypto.randomUUID();
}

function generateToken() {
  return crypto.randomBytes(32).toString('hex');
}

module.exports = { getDb, generateId, generateToken };
