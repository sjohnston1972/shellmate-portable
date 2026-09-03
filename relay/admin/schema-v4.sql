-- Indexes and constraints from the September 2026 review. Apply after
-- schema-v3.sql:
--   wrangler d1 execute shellmate-licences --remote --file=schema-v4.sql
--
-- Each statement says which finding it answers. All are IF NOT EXISTS, so
-- the file can be applied again without harm.

-- #509: the licence list is newest first with a created_at:id cursor. Without
-- this, every page was a full sort of the table.
CREATE INDEX IF NOT EXISTS idx_licences_created ON licences(created_at DESC, id DESC);
