-- Portal v2 additions. Apply after schema.sql:
--   wrangler d1 execute shellmate-licences --remote --file=schema-v2.sql

CREATE TABLE IF NOT EXISTS settings (
  key    TEXT PRIMARY KEY,
  value  TEXT NOT NULL DEFAULT ''
);

INSERT OR IGNORE INTO settings (key, value) VALUES
  ('requests_enabled', '0'),        -- the public request page issues keys on its own
  ('request_days',     '30'),       -- how long a self-requested key lasts
  ('request_kind',     'person'),
  ('mail_from',        'ShellMate <licences@foundry-ns.com>'),
  ('mail_subject',     'Your ShellMate licence key'),
  ('mail_intro',       'Thank you. Your ShellMate licence key is below. Paste it under Settings → Licence in ShellMate, or save this message as a .key file and import it.'),
  ('portal_notice',    '');

ALTER TABLE licences ADD COLUMN last_sent INTEGER;
ALTER TABLE licences ADD COLUMN sent_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE licences ADD COLUMN source TEXT NOT NULL DEFAULT 'admin';   -- admin | request
