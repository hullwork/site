-- The tickets table the "tickets" entity in hull.yaml declares. Row-level security is
-- FORCEd, not merely enabled: ENABLE alone still lets the table owner (the role
-- migrations typically run as) bypass every policy, and this app's runtime role must
-- never be able to see another owner's rows just by being the table owner or a
-- superuser role in the same connection.
CREATE TABLE tickets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    owner_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE tickets ENABLE ROW LEVEL SECURITY;
ALTER TABLE tickets FORCE ROW LEVEL SECURITY;

-- hull.user_id is set by db.withUser() (sdk/ts/src/index.ts) inside the same transaction
-- as every query the app runs on behalf of an end user (contract's "SDK" section).
CREATE POLICY tickets_owner_isolation ON tickets
    USING (owner_id = current_setting('hull.user_id', true));
