CREATE TABLE IF NOT EXISTS accounts (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  domain TEXT,
  acct TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  avatar TEXT NOT NULL DEFAULT '',
  header TEXT NOT NULL DEFAULT '',
  fields_json TEXT NOT NULL DEFAULT '[]',
  locked INTEGER NOT NULL DEFAULT 0,
  bot INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  password_hash TEXT,
  email TEXT,
  is_admin INTEGER NOT NULL DEFAULT 0,
  is_local INTEGER NOT NULL DEFAULT 1,
  url TEXT NOT NULL DEFAULT '',
  statuses_count INTEGER NOT NULL DEFAULT 0,
  followers_count INTEGER NOT NULL DEFAULT 0,
  following_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_accounts_username ON accounts(username);

CREATE TABLE IF NOT EXISTS oauth_apps (
  id TEXT PRIMARY KEY,
  client_id TEXT NOT NULL UNIQUE,
  client_secret TEXT NOT NULL,
  name TEXT NOT NULL,
  redirect_uri TEXT NOT NULL,
  scopes TEXT NOT NULL DEFAULT 'read',
  website TEXT,
  vapid_key TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_codes (
  code TEXT PRIMARY KEY,
  app_id TEXT NOT NULL,
  account_id TEXT,
  scopes TEXT NOT NULL,
  redirect_uri TEXT NOT NULL,
  code_challenge TEXT,
  code_challenge_method TEXT,
  created_at INTEGER NOT NULL,
  used INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS oauth_tokens (
  token TEXT PRIMARY KEY,
  app_id TEXT NOT NULL,
  account_id TEXT,
  scopes TEXT NOT NULL,
  token_type TEXT NOT NULL DEFAULT 'Bearer',
  created_at INTEGER NOT NULL,
  revoked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tokens_account ON oauth_tokens(account_id);

CREATE TABLE IF NOT EXISTS sessions (
  sid TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  csrf TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS statuses (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  content TEXT NOT NULL DEFAULT '',
  spoiler_text TEXT NOT NULL DEFAULT '',
  visibility TEXT NOT NULL DEFAULT 'public',
  sensitive INTEGER NOT NULL DEFAULT 0,
  in_reply_to_id TEXT,
  in_reply_to_account_id TEXT,
  reblog_of_id TEXT,
  language TEXT,
  created_at TEXT NOT NULL,
  edited_at TEXT,
  uri TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  is_local INTEGER NOT NULL DEFAULT 1,
  application_id TEXT,
  poll_id TEXT,
  favourites_count INTEGER NOT NULL DEFAULT 0,
  reblogs_count INTEGER NOT NULL DEFAULT 0,
  replies_count INTEGER NOT NULL DEFAULT 0,
  deleted INTEGER NOT NULL DEFAULT 0,
  idempotency_key TEXT
);
CREATE INDEX IF NOT EXISTS idx_statuses_account ON statuses(account_id);
CREATE INDEX IF NOT EXISTS idx_statuses_created ON statuses(created_at);
CREATE INDEX IF NOT EXISTS idx_statuses_reply ON statuses(in_reply_to_id);
CREATE INDEX IF NOT EXISTS idx_statuses_reblog ON statuses(reblog_of_id);
CREATE INDEX IF NOT EXISTS idx_statuses_idem ON statuses(idempotency_key);

CREATE TABLE IF NOT EXISTS status_edits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  status_id TEXT NOT NULL,
  content TEXT NOT NULL,
  spoiler_text TEXT NOT NULL DEFAULT '',
  sensitive INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edits_status ON status_edits(status_id);

CREATE TABLE IF NOT EXISTS media (
  id TEXT PRIMARY KEY,
  account_id TEXT,
  status_id TEXT,
  type TEXT NOT NULL DEFAULT 'image',
  url TEXT NOT NULL DEFAULT '',
  preview_url TEXT NOT NULL DEFAULT '',
  description TEXT,
  meta TEXT NOT NULL DEFAULT '{}',
  blurhash TEXT,
  file_path TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_status ON media(status_id);

CREATE TABLE IF NOT EXISTS follows (
  follower_id TEXT NOT NULL,
  target_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  show_reblogs INTEGER NOT NULL DEFAULT 1,
  notify INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(follower_id, target_id)
);
CREATE INDEX IF NOT EXISTS idx_follows_target ON follows(target_id);

CREATE TABLE IF NOT EXISTS follow_requests (
  follower_id TEXT NOT NULL,
  target_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(follower_id, target_id)
);

CREATE TABLE IF NOT EXISTS blocks (
  account_id TEXT NOT NULL,
  target_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(account_id, target_id)
);

CREATE TABLE IF NOT EXISTS mutes (
  account_id TEXT NOT NULL,
  target_id TEXT NOT NULL,
  notifications INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  PRIMARY KEY(account_id, target_id)
);

CREATE TABLE IF NOT EXISTS favourites (
  account_id TEXT NOT NULL,
  status_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(account_id, status_id)
);
CREATE INDEX IF NOT EXISTS idx_fav_status ON favourites(status_id);

CREATE TABLE IF NOT EXISTS bookmarks (
  account_id TEXT NOT NULL,
  status_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(account_id, status_id)
);

CREATE TABLE IF NOT EXISTS notifications (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  from_account_id TEXT NOT NULL,
  type TEXT NOT NULL,
  status_id TEXT,
  created_at TEXT NOT NULL,
  read INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_notif_account ON notifications(account_id, created_at DESC);

CREATE TABLE IF NOT EXISTS lists (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  title TEXT NOT NULL,
  replies_policy TEXT NOT NULL DEFAULT 'list',
  exclusive INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS list_accounts (
  list_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  PRIMARY KEY(list_id, account_id)
);

CREATE TABLE IF NOT EXISTS hashtags (
  name TEXT PRIMARY KEY,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS status_tags (
  status_id TEXT NOT NULL,
  tag TEXT NOT NULL,
  PRIMARY KEY(status_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_status_tags_tag ON status_tags(tag);

CREATE TABLE IF NOT EXISTS status_mentions (
  status_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  PRIMARY KEY(status_id, account_id)
);

CREATE TABLE IF NOT EXISTS polls (
  id TEXT PRIMARY KEY,
  status_id TEXT,
  account_id TEXT NOT NULL,
  expires_at TEXT,
  multiple INTEGER NOT NULL DEFAULT 0,
  hide_totals INTEGER NOT NULL DEFAULT 0,
  voters_count INTEGER NOT NULL DEFAULT 0,
  votes_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS poll_options (
  poll_id TEXT NOT NULL,
  idx INTEGER NOT NULL,
  title TEXT NOT NULL,
  votes_count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(poll_id, idx)
);

CREATE TABLE IF NOT EXISTS poll_votes (
  poll_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  idx INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(poll_id, account_id, idx)
);

CREATE TABLE IF NOT EXISTS reports (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  target_id TEXT NOT NULL,
  status_ids TEXT NOT NULL DEFAULT '[]',
  comment TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL DEFAULT 'other',
  forwarded INTEGER NOT NULL DEFAULT 0,
  action_taken INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor_id TEXT,
  action TEXT NOT NULL,
  target TEXT,
  meta TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  queue TEXT NOT NULL,
  payload TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  ran_at TEXT
);

CREATE TABLE IF NOT EXISTS markers (
  account_id TEXT NOT NULL,
  timeline TEXT NOT NULL,
  last_read_id TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(account_id, timeline)
);

CREATE TABLE IF NOT EXISTS filters (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  phrase TEXT NOT NULL,
  context TEXT NOT NULL DEFAULT '[]',
  whole_word INTEGER NOT NULL DEFAULT 0,
  expires_at TEXT,
  irreversible INTEGER NOT NULL DEFAULT 0
);
