-- Installations (#451). Apply after schema-v2.sql:
--   wrangler d1 execute shellmate-licences --remote --file=schema-v3.sql
--
-- One row per (licence, machine). A copy of ShellMate reports itself when a
-- key is entered and again at every refresh; removing the key marks the row
-- rather than deleting it, so the history stays readable.

CREATE TABLE IF NOT EXISTS activations (
  licence_id  TEXT NOT NULL REFERENCES licences(id) ON DELETE CASCADE,
  machine_id  TEXT NOT NULL,                  -- hash of hostname, user and architecture, made by the app
  hostname    TEXT NOT NULL DEFAULT '',
  user        TEXT NOT NULL DEFAULT '',
  platform    TEXT NOT NULL DEFAULT '',
  version     TEXT NOT NULL DEFAULT '',       -- the ShellMate version last seen
  first_seen  INTEGER NOT NULL,
  last_seen   INTEGER NOT NULL,
  seen_count  INTEGER NOT NULL DEFAULT 1,
  removed_at  INTEGER,                        -- set when the copy removes its key
  last_ip     TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (licence_id, machine_id)
);

CREATE INDEX IF NOT EXISTS idx_activations_seen ON activations(last_seen DESC);
