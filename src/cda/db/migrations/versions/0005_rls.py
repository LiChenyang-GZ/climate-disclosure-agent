"""Read-only tool role and team-scoped row security."""

from alembic import op

revision = "0005_rls"
down_revision = "0004_unit_seq"
branch_labels = None
depends_on = None

_TABLES = ("packs", "team_members", "documents", "document_parts", "units")


def upgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cda_tool') THEN
            CREATE ROLE cda_tool NOLOGIN;
        END IF;
    END $$""")
    op.execute("GRANT cda_tool TO current_user")
    op.execute("GRANT USAGE ON SCHEMA public TO cda_tool")
    op.execute("GRANT SELECT ON packs, team_members, documents, document_parts, units TO cda_tool")
    op.execute("""CREATE FUNCTION cda_current_user() RETURNS uuid
        LANGUAGE sql STABLE AS $$
            SELECT NULLIF(current_setting('cda.user_id', true), '')::uuid
        $$""")
    predicates = {
        "team_members": "user_id = cda_current_user()",
        "packs": "team_id IN (SELECT team_id FROM team_members)",
        "documents": "pack_id IN (SELECT id FROM packs)",
        "document_parts": "document_id IN (SELECT id FROM documents)",
        "units": "document_id IN (SELECT id FROM documents)",
    }
    for table, predicate in predicates.items():
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY cda_tool_select ON {table} FOR SELECT TO cda_tool USING ({predicate})"
        )


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"DROP POLICY cda_tool_select ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE SELECT ON {table} FROM cda_tool")
    op.execute("REVOKE USAGE ON SCHEMA public FROM cda_tool")
    op.execute("REVOKE cda_tool FROM current_user")
    op.execute("DROP FUNCTION cda_current_user()")
    op.execute("""DO $$ BEGIN
        DROP ROLE IF EXISTS cda_tool;
    EXCEPTION WHEN dependent_objects_still_exist THEN
        NULL;
    END $$""")
