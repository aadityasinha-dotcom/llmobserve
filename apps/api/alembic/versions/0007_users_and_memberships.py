"""users, project memberships, and the functions that are the only way to write them

Revision ID: 0007_users_memberships
Revises: 0006_scoped_api_keys
Create Date: 2026-09-22

Human sign-in for the dashboard. Until now the only identity was a project API
key, so the dashboard had to hold one and anyone who could open it saw that
project's traces. After this migration a person signs in with Google, and sees
exactly the projects they are a member of.

Tenancy does not move. A trace still belongs to a project and row-level security
still keys on `app.current_project_id`. What is new is the step before it: a
signed-in user may only select a project they hold a membership in.

The write path is functions, not grants
---------------------------------------
Self-serve sign-up means the running application must now create users,
projects, memberships and API keys - rows it could previously only read. The
obvious way to allow that is `GRANT INSERT` on those tables, and it would turn a
bug anywhere in the API into "mint a key for any project".

Instead the application role gets EXECUTE on a handful of SECURITY DEFINER
functions and still has no INSERT or UPDATE on any of these tables. Each function
checks membership itself, so the rule "you can only create or revoke keys for a
project you belong to" is enforced by Postgres, in the same place row-level
security enforces "you can only read your own project's traces" (CLAUDE.md,
rule 2), rather than by a WHERE clause in Python.

Every function pins `search_path`. A SECURITY DEFINER function that resolves
names through the caller's search_path can be hijacked by an object created in a
schema earlier on that path; pinning it is the standard defence.

Like `projects` and `api_keys`, `users` and `project_members` sit outside
row-level security: authentication reads them before any tenant is known.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_users_memberships"
down_revision: str | None = "0006_scoped_api_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "llmobserve_app"

_FUNCTIONS = (
    "auth_sign_in_google(text, text, boolean, text, text)",
    "auth_create_api_key(uuid, uuid, text, text, text, text[])",
    "auth_revoke_api_key(uuid, uuid)",
    "auth_list_api_keys(uuid, uuid)",
    "auth_bump_session_version(uuid)",
)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # Google's stable subject identifier. Email is NOT the identity: a Google
        # account's address can change, and two accounts can briefly share one.
        sa.Column("google_sub", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        # Embedded in every session token and compared on every request.
        # Incrementing it invalidates all of a user's sessions at once, which is
        # what gives stateless tokens a working sign-out.
        sa.Column("session_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_login_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("google_sub", name="uq_users_google_sub"),
    )
    # Operator lookups by address (granting membership to an existing project).
    op.create_index("ix_users_lower_email", "users", [sa.text("lower(email)")])

    op.create_table(
        "project_members",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.Text(), server_default=sa.text("'member'"), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("project_id", "user_id", name="pk_project_members"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_project_members_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_project_members_user_id_users",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("role IN ('owner', 'member')", name="ck_project_members_role"),
    )
    # "Which projects can this user see" runs on every dashboard request.
    op.create_index("ix_project_members_user_id", "project_members", ["user_id"])

    op.add_column(
        "projects",
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_projects_created_by_users",
        "projects",
        "users",
        ["created_by"],
        ["id"],
        ondelete="SET NULL",
    )

    # ------------------------------------------------------------------
    # Sign-in. Upsert by Google subject; on the first sign-in, also create the
    # user's personal project and make them its owner, in the same transaction,
    # so a user can never exist without somewhere to send traces.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE FUNCTION auth_sign_in_google(
            p_sub text, p_email text, p_email_verified boolean, p_name text, p_avatar text
        )
        RETURNS TABLE (user_id uuid, session_version integer, created boolean)
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_user users%ROWTYPE;
            v_project uuid;
        BEGIN
            -- Defence in depth: the API already refuses unverified addresses.
            -- An unverified Google email is an address someone typed, not one
            -- they proved they own.
            IF NOT p_email_verified THEN
                RAISE EXCEPTION 'email not verified' USING ERRCODE = 'insufficient_privilege';
            END IF;

            SELECT * INTO v_user FROM users WHERE google_sub = p_sub FOR UPDATE;

            IF FOUND THEN
                UPDATE users
                   SET email = p_email, name = p_name, avatar_url = p_avatar,
                       last_login_at = now()
                 WHERE id = v_user.id;
                RETURN QUERY SELECT v_user.id, v_user.session_version, false;
                RETURN;
            END IF;

            -- ON CONFLICT covers two first sign-ins racing (a double-clicked
            -- button, two tabs): the loser finds the winner's row instead of
            -- failing on the unique constraint.
            INSERT INTO users (google_sub, email, name, avatar_url, last_login_at)
            VALUES (p_sub, p_email, p_name, p_avatar, now())
            ON CONFLICT (google_sub) DO NOTHING
            RETURNING * INTO v_user;

            IF NOT FOUND THEN
                SELECT * INTO v_user FROM users WHERE google_sub = p_sub;
                RETURN QUERY SELECT v_user.id, v_user.session_version, false;
                RETURN;
            END IF;

            INSERT INTO projects (name, created_by)
            VALUES (coalesce(nullif(p_name, ''), p_email) || '''s project', v_user.id)
            RETURNING id INTO v_project;

            INSERT INTO project_members (project_id, user_id, role)
            VALUES (v_project, v_user.id, 'owner');

            RETURN QUERY SELECT v_user.id, v_user.session_version, true;
        END;
        $$;
        """
    )

    # ------------------------------------------------------------------
    # API keys, on behalf of a signed-in user. Each one checks membership
    # itself; the caller's say-so about which project it may touch is never
    # enough.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE FUNCTION auth_create_api_key(
            p_user uuid, p_project uuid, p_hash text, p_prefix text, p_label text, p_scopes text[]
        )
        RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_key uuid;
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM project_members WHERE project_id = p_project AND user_id = p_user
            ) THEN
                RAISE EXCEPTION 'not a member of this project'
                      USING ERRCODE = 'insufficient_privilege';
            END IF;

            INSERT INTO api_keys (project_id, key_hash, key_prefix, label, scopes)
            VALUES (p_project, p_hash, p_prefix, p_label, p_scopes)
            RETURNING id INTO v_key;
            RETURN v_key;
        END;
        $$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION auth_revoke_api_key(p_user uuid, p_key uuid)
        RETURNS boolean
        LANGUAGE sql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            WITH revoked AS (
                UPDATE api_keys k
                   SET revoked_at = now()
                 WHERE k.id = p_key
                   AND k.revoked_at IS NULL
                   AND EXISTS (
                        SELECT 1 FROM project_members m
                         WHERE m.project_id = k.project_id AND m.user_id = p_user
                   )
                RETURNING 1
            )
            SELECT EXISTS (SELECT 1 FROM revoked);
        $$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION auth_list_api_keys(p_user uuid, p_project uuid)
        RETURNS TABLE (
            id uuid, key_prefix text, label text, scopes text[],
            created_at timestamptz, revoked_at timestamptz
        )
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM project_members WHERE project_id = p_project AND user_id = p_user
            ) THEN
                RAISE EXCEPTION 'not a member of this project'
                      USING ERRCODE = 'insufficient_privilege';
            END IF;

            -- Never the hash. The prefix is enough to tell keys apart.
            RETURN QUERY
                SELECT k.id, k.key_prefix::text, k.label::text, k.scopes, k.created_at, k.revoked_at
                  FROM api_keys k
                 WHERE k.project_id = p_project
                 ORDER BY k.created_at;
        END;
        $$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION auth_bump_session_version(p_user uuid)
        RETURNS integer
        LANGUAGE sql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            UPDATE users SET session_version = session_version + 1
             WHERE id = p_user
            RETURNING session_version;
        $$;
        """
    )

    # Reads the API needs to authenticate a session and list its projects.
    op.execute(f"GRANT SELECT ON users, project_members TO {APP_ROLE};")

    # Functions are executable by PUBLIC by default. Restrict them to the
    # application role explicitly, so only the API can call them.
    for signature in _FUNCTIONS:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC;")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {APP_ROLE};")


def downgrade() -> None:
    for signature in _FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {signature};")
    op.drop_constraint("fk_projects_created_by_users", "projects", type_="foreignkey")
    op.drop_column("projects", "created_by")
    op.drop_index("ix_project_members_user_id", table_name="project_members")
    op.drop_table("project_members")
    op.drop_index("ix_users_lower_email", table_name="users")
    op.drop_table("users")
