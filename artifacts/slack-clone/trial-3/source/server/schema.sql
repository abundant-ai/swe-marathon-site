-- Slack-like chat schema. SQLite WAL mode.

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    timezone TEXT NOT NULL DEFAULT 'UTC',
    avatar_url TEXT NOT NULL DEFAULT '',
    status_text TEXT NOT NULL DEFAULT '',
    status_emoji TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tokens (
    token TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tokens_user ON tokens(user_id);

CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    join_mode TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspace_members (
    workspace_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    joined_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_wm_user ON workspace_members(user_id);

CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    name TEXT NOT NULL,
    is_private INTEGER NOT NULL DEFAULT 0,
    is_dm INTEGER NOT NULL DEFAULT 0,
    topic TEXT NOT NULL DEFAULT '',
    is_archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    dm_key TEXT UNIQUE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_channels_ws_name ON channels(workspace_id, name);
CREATE INDEX IF NOT EXISTS idx_channels_ws ON channels(workspace_id);

CREATE TABLE IF NOT EXISTS channel_members (
    channel_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    joined_at TEXT NOT NULL,
    PRIMARY KEY (channel_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_cm_user ON channel_members(user_id);

CREATE TABLE IF NOT EXISTS channel_seq (
    channel_id TEXT PRIMARY KEY,
    next_seq INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    author_id TEXT NOT NULL,
    body TEXT NOT NULL,
    parent_id TEXT,
    seq INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    edited_at TEXT,
    deleted_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_chan_seq ON messages(channel_id, seq);
CREATE INDEX IF NOT EXISTS idx_messages_chan ON messages(channel_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_parent ON messages(parent_id);
CREATE INDEX IF NOT EXISTS idx_messages_author ON messages(author_id);

CREATE TABLE IF NOT EXISTS reactions (
    message_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    emoji TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (message_id, user_id, emoji)
);
CREATE INDEX IF NOT EXISTS idx_reactions_msg ON reactions(message_id);

CREATE TABLE IF NOT EXISTS pins (
    message_id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    pinned_by TEXT NOT NULL,
    pinned_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    uploader_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    size INTEGER NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    attached_to TEXT
);

CREATE TABLE IF NOT EXISTS message_files (
    message_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    PRIMARY KEY (message_id, file_id)
);

CREATE TABLE IF NOT EXISTS mentions (
    message_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    PRIMARY KEY (message_id, user_id)
);

CREATE TABLE IF NOT EXISTS groups (
    workspace_id TEXT NOT NULL,
    handle TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, handle)
);

CREATE TABLE IF NOT EXISTS group_members (
    workspace_id TEXT NOT NULL,
    handle TEXT NOT NULL,
    user_id TEXT NOT NULL,
    PRIMARY KEY (workspace_id, handle, user_id)
);

CREATE TABLE IF NOT EXISTS invitations (
    code TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    invited_by TEXT NOT NULL,
    email TEXT,
    invited_username TEXT,
    expires_at TEXT,
    max_uses INTEGER NOT NULL DEFAULT 1,
    used_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS read_state (
    user_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    last_read_seq INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, channel_id)
);

CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_chan_seq ON events(channel_id, seq);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_chan_seq_uniq ON events(channel_id, seq);
