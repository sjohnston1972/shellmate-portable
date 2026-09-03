-- ShellMate licence service — D1 schema (#447).
-- Apply with: wrangler d1 execute shellmate-licences --remote --file=schema.sql

CREATE TABLE IF NOT EXISTS users (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL DEFAULT '',
  email       TEXT NOT NULL DEFAULT '',
  org         TEXT NOT NULL DEFAULT '',
  notes       TEXT NOT NULL DEFAULT '',
  created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS licences (
  id              TEXT PRIMARY KEY,
  user_id         TEXT REFERENCES users(id) ON DELETE SET NULL,
  kind            TEXT NOT NULL,              -- person | org
  licensee        TEXT NOT NULL,
  email           TEXT NOT NULL DEFAULT '',
  seats           INTEGER NOT NULL DEFAULT 1,
  issued          TEXT NOT NULL,              -- ISO date
  expires         TEXT NOT NULL DEFAULT '',   -- ISO date, '' = perpetual
  grace_days      INTEGER NOT NULL DEFAULT 14,
  features        TEXT NOT NULL DEFAULT '["updates"]',
  token           TEXT NOT NULL,              -- the signed key as issued
  revoked         INTEGER NOT NULL DEFAULT 0,
  revoked_reason  TEXT NOT NULL DEFAULT '',
  notes           TEXT NOT NULL DEFAULT '',
  created_at      INTEGER NOT NULL,
  last_refresh    INTEGER,
  refresh_count   INTEGER NOT NULL DEFAULT 0,
  last_ip         TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_licences_user ON licences(user_id);
CREATE INDEX IF NOT EXISTS idx_licences_email ON licences(email);

CREATE TABLE IF NOT EXISTS events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  licence_id  TEXT,
  at          INTEGER NOT NULL,
  kind        TEXT NOT NULL,                  -- issued | renewed | revoked | restored | refreshed | deleted | login
  detail      TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_events_licence ON events(licence_id, at DESC);
