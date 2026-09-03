-- Indexes and constraints from the September 2026 review. Apply after
-- schema-v3.sql:
--   wrangler d1 execute shellmate-licences --remote --file=schema-v4.sql
--
-- Each statement says which finding it answers. All are IF NOT EXISTS, so
-- the file can be applied again without harm.

-- #509: the licence list is newest first with a created_at:id cursor. Without
-- this, every page was a full sort of the table.
CREATE INDEX IF NOT EXISTS idx_licences_created ON licences(created_at DESC, id DESC);

-- #510: every lookup by address is lower(email) = lower(?), which a plain
-- index on the column cannot serve. Expression indexes can. The unique one
-- on users is also what stops two concurrent requests for one address
-- creating two people (#512): the second INSERT is ignored and the existing
-- row is used. It refuses to build while duplicates exist — find them with
--   SELECT lower(email), COUNT(*) FROM users WHERE email != ''
--   GROUP BY lower(email) HAVING COUNT(*) > 1;
-- and merge them (move the licences' user_id, delete the spare) first.
CREATE INDEX IF NOT EXISTS idx_licences_email_lc ON licences(lower(email));
CREATE INDEX IF NOT EXISTS idx_users_email_lc ON users(lower(email));
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_unique ON users(lower(email)) WHERE email != '';
