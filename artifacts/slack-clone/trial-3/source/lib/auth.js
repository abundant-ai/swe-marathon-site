const crypto = require('crypto');
const { getDb, generateToken } = require('./db');

function hashPassword(password) {
  const salt = crypto.randomBytes(16).toString('hex');
  const hash = crypto.pbkdf2Sync(password, salt, 100000, 64, 'sha512').toString('hex');
  return `${salt}:${hash}`;
}

function verifyPassword(password, stored) {
  const [salt, hash] = stored.split(':');
  const verify = crypto.pbkdf2Sync(password, salt, 100000, 64, 'sha512').toString('hex');
  return hash === verify;
}

function authMiddleware(req, res, next) {
  const authHeader = req.headers.authorization;
  if (!authHeader || !authHeader.startsWith('Bearer ')) {
    return res.status(401).json({ error: 'Unauthorized' });
  }
  const token = authHeader.slice(7);
  const db = getDb();
  const row = db.prepare(`
    SELECT t.token, t.user_id, u.username, u.display_name, u.timezone, u.avatar_url, u.status_text, u.status_emoji, u.created_at
    FROM tokens t JOIN users u ON t.user_id = u.id
    WHERE t.token = ?
  `).get(token);
  if (!row) {
    return res.status(401).json({ error: 'Invalid token' });
  }
  req.user = {
    id: row.user_id,
    username: row.username,
    display_name: row.display_name,
    timezone: row.timezone,
    avatar_url: row.avatar_url,
    status_text: row.status_text,
    status_emoji: row.status_emoji,
    created_at: row.created_at
  };
  req.token = token;
  next();
}

function optionalAuth(req, res, next) {
  const authHeader = req.headers.authorization;
  if (authHeader && authHeader.startsWith('Bearer ')) {
    const token = authHeader.slice(7);
    const db = getDb();
    const row = db.prepare(`
      SELECT t.token, t.user_id, u.username, u.display_name, u.timezone, u.avatar_url, u.status_text, u.status_emoji, u.created_at
      FROM tokens t JOIN users u ON t.user_id = u.id
      WHERE t.token = ?
    `).get(token);
    if (row) {
      req.user = {
        id: row.user_id,
        username: row.username,
        display_name: row.display_name,
        timezone: row.timezone,
        avatar_url: row.avatar_url,
        status_text: row.status_text,
        status_emoji: row.status_emoji,
        created_at: row.created_at
      };
      req.token = token;
    }
  }
  next();
}

module.exports = { hashPassword, verifyPassword, authMiddleware, optionalAuth, generateToken };
