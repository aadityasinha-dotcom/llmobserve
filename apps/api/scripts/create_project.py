#!/usr/bin/env python
"""Issue a project and its API key.

    python scripts/create_project.py my-project

Writes to `projects`, which the runtime role can only read - authentication
resolves a key against it before any tenant is known, so it is deliberately
outside row-level security and the application role holds SELECT and nothing
else. This script therefore needs the owner connection: MIGRATION_DATABASE_URL,
or SUPABASE_MIGRATION_URL when pointed at Supabase.

The raw key is printed once and never stored. Only its SHA-256 hash goes to the
database, so a dump of `projects` cannot be replayed against the API.
"""

import argparse
import asyncio
import os
import secrets
import sys
from pathlib import Path
from uuid import uuid4

# Importable when run as `python scripts/create_project.py` from apps/api.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.deps import hash_api_key  # noqa: E402

KEY_PREFIX = "llmo_sk_"


def _admin_url() -> str:
    for var in ("MIGRATION_DATABASE_URL", "SUPABASE_MIGRATION_URL"):
        url = os.environ.get(var)
        if url:
            return url
    raise SystemExit(
        "Set MIGRATION_DATABASE_URL (local) or SUPABASE_MIGRATION_URL (Supabase).\n"
        "This needs the owner connection: the application role cannot write `projects`."
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="Project name, e.g. local-dev")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Delete an existing project of the same name first. Cascades to its "
        "traces and observations - this destroys data.",
    )
    args = parser.parse_args()

    api_key = KEY_PREFIX + secrets.token_urlsafe(32)
    project_id = uuid4()

    engine = create_async_engine(_admin_url())
    try:
        async with engine.begin() as conn:
            if args.replace:
                await conn.execute(
                    text("DELETE FROM projects WHERE name = :n"), {"n": args.name}
                )
            existing = await conn.scalar(
                text("SELECT id FROM projects WHERE name = :n"), {"n": args.name}
            )
            if existing is not None:
                raise SystemExit(
                    f"Project {args.name!r} already exists ({existing}).\n"
                    "Its key cannot be recovered - only the hash is stored. Re-run with "
                    "--replace to issue a new one, which DELETES its traces."
                )
            await conn.execute(
                text(
                    "INSERT INTO projects (id, name, api_key_hash, api_key_prefix) "
                    "VALUES (:i, :n, :h, :p)"
                ),
                {
                    "i": project_id,
                    "n": args.name,
                    "h": hash_api_key(api_key),
                    "p": api_key[:16],
                },
            )
    finally:
        await engine.dispose()

    print(f"project    {args.name}")
    print(f"project_id {project_id}")
    print(f"api_key    {api_key}")
    print("\nShown once. Export it for the SDK:")
    print(f"  export LLMOBSERVE_API_KEY={api_key}")


if __name__ == "__main__":
    asyncio.run(main())
