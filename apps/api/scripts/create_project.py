#!/usr/bin/env python
"""Issue projects and their API keys.

    python scripts/create_project.py my-project                    # project + full key
    python scripts/create_project.py my-project --add --scopes ingest --label sdk
    python scripts/create_project.py my-project --add --scopes read  --label dashboard
    python scripts/create_project.py my-project --rotate           # replace every key
    python scripts/create_project.py my-project --revoke sdk       # revoke by label

A project may hold any number of keys, each with its own scopes, so the
credential that ships inside a client application need not be the one that can
read every stored prompt back out. Prefer two narrow keys over one broad one:

    ingest  POST /v1/ingest
    read    GET  /v1/traces, GET /v1/traces/{id}

Writes to `api_keys` and `projects`, which the runtime role can only read -
authentication resolves a key against them before any tenant is known, so they
sit outside row-level security and the application role holds SELECT and nothing
else. This script therefore needs the owner connection: MIGRATION_DATABASE_URL,
or SUPABASE_MIGRATION_URL when pointed at Supabase.

Raw keys are printed once and never stored. Only their SHA-256 hash goes to the
database, so a dump of `api_keys` cannot be replayed against the API.
"""

import argparse
import asyncio
import os
import secrets
import sys
from pathlib import Path
from uuid import UUID, uuid4

# Importable when run as `python scripts/create_project.py` from apps/api.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.deps import hash_api_key
from app.models import VALID_SCOPES

KEY_PREFIX = "llmo_sk_"


def _admin_url() -> str:
    for var in ("MIGRATION_DATABASE_URL", "SUPABASE_MIGRATION_URL"):
        url = os.environ.get(var)
        if url:
            return url
    raise SystemExit(
        "Set MIGRATION_DATABASE_URL (local) or SUPABASE_MIGRATION_URL (Supabase).\n"
        "This needs the owner connection: the application role cannot write `api_keys`."
    )


def _parse_scopes(raw: str) -> list[str]:
    scopes = [part.strip() for part in raw.split(",") if part.strip()]
    unknown = sorted(set(scopes) - VALID_SCOPES)
    if unknown:
        raise SystemExit(
            f"Unknown scope(s): {', '.join(unknown)}. Valid: {', '.join(sorted(VALID_SCOPES))}"
        )
    if not scopes:
        raise SystemExit("--scopes needs at least one of: " + ", ".join(sorted(VALID_SCOPES)))
    return scopes


async def _issue_key(
    conn: AsyncConnection, project_id: UUID, scopes: list[str], label: str | None
) -> str:
    api_key = KEY_PREFIX + secrets.token_urlsafe(32)
    await conn.execute(
        text(
            "INSERT INTO api_keys (project_id, key_hash, key_prefix, label, scopes) "
            "VALUES (:p, :h, :pre, :l, :s)"
        ),
        {
            "p": project_id,
            "h": hash_api_key(api_key),
            "pre": api_key[:16],
            "l": label,
            "s": scopes,
        },
    )
    return api_key


async def _project_id(conn: AsyncConnection, name: str) -> UUID | None:
    return await conn.scalar(text("SELECT id FROM projects WHERE name = :n"), {"n": name})


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="Project name, e.g. local-dev")
    parser.add_argument(
        "--scopes",
        default="ingest,read",
        help="Comma-separated scopes for the new key: ingest, read. "
        "Default is both, which suits a single-consumer setup.",
    )
    parser.add_argument("--label", help="What the key is for: sdk, dashboard, ci.")
    parser.add_argument(
        "--add",
        action="store_true",
        help="Issue an ADDITIONAL key for an existing project, leaving its other "
        "keys working. This is how the SDK and the dashboard get separate keys.",
    )
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="Revoke every key on the project and issue one new one. Keeps the "
        "project id and all of its traces.",
    )
    parser.add_argument(
        "--revoke",
        metavar="LABEL",
        help="Revoke the project's keys carrying this label and issue nothing.",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Delete an existing project of the same name first. Cascades to its "
        "traces and observations - this destroys data.",
    )
    parser.add_argument("--list", action="store_true", help="List the project's keys and exit.")
    args = parser.parse_args()

    exclusive = [args.replace, args.rotate, args.add, bool(args.revoke), args.list]
    if sum(bool(flag) for flag in exclusive) > 1:
        raise SystemExit("--add, --rotate, --revoke, --replace and --list are mutually exclusive.")

    scopes = _parse_scopes(args.scopes)
    engine = create_async_engine(_admin_url())
    try:
        async with engine.begin() as conn:
            existing = await _project_id(conn, args.name)

            if args.list:
                await _print_keys(conn, args.name, existing)
                return

            if args.revoke:
                if existing is None:
                    raise SystemExit(f"No project named {args.name!r}.")
                count = await conn.scalar(
                    text(
                        "WITH revoked AS ("
                        "  UPDATE api_keys SET revoked_at = now() "
                        "   WHERE project_id = :p AND label = :l AND revoked_at IS NULL"
                        "   RETURNING 1) SELECT count(*) FROM revoked"
                    ),
                    {"p": existing, "l": args.revoke},
                )
                print(f"revoked {count} key(s) labelled {args.revoke!r} on {args.name}")
                print("Traces are untouched; only the credential stops working.")
                return

            if args.add:
                if existing is None:
                    raise SystemExit(f"No project named {args.name!r}. Drop --add to create it.")
                api_key = await _issue_key(conn, existing, scopes, args.label)
                _report(args.name, existing, api_key, scopes, args.label, "new key added")
                return

            if args.rotate:
                if existing is None:
                    raise SystemExit(f"No project named {args.name!r}. Drop --rotate to create it.")
                await conn.execute(
                    text(
                        "UPDATE api_keys SET revoked_at = now() "
                        "WHERE project_id = :p AND revoked_at IS NULL"
                    ),
                    {"p": existing},
                )
                api_key = await _issue_key(conn, existing, scopes, args.label)
                _report(args.name, existing, api_key, scopes, args.label, "rotated, traces kept")
                return

            if args.replace:
                await conn.execute(text("DELETE FROM projects WHERE name = :n"), {"n": args.name})
                existing = None

            if existing is not None:
                raise SystemExit(
                    f"Project {args.name!r} already exists ({existing}).\n"
                    "Keys cannot be recovered - only their hashes are stored.\n"
                    "  --add      issue an ADDITIONAL key, keeping the existing ones\n"
                    "  --rotate   revoke every key and issue one, KEEPING its traces\n"
                    "  --replace  recreate the project, DELETING its traces"
                )

            project_id = uuid4()
            await conn.execute(
                text("INSERT INTO projects (id, name) VALUES (:i, :n)"),
                {"i": project_id, "n": args.name},
            )
            api_key = await _issue_key(conn, project_id, scopes, args.label)
            _report(args.name, project_id, api_key, scopes, args.label, "project created")
    finally:
        await engine.dispose()


async def _print_keys(conn: AsyncConnection, name: str, project_id: UUID | None) -> None:
    if project_id is None:
        raise SystemExit(f"No project named {name!r}.")
    rows = (
        await conn.execute(
            text(
                "SELECT key_prefix, label, scopes, created_at, revoked_at "
                "FROM api_keys WHERE project_id = :p ORDER BY created_at"
            ),
            {"p": project_id},
        )
    ).all()
    print(f"project    {name}")
    print(f"project_id {project_id}\n")
    print(f"{'prefix':<18} {'label':<12} {'scopes':<16} status")
    for prefix, label, key_scopes, _created, revoked in rows:
        status = "revoked" if revoked else "active"
        print(f"{(prefix or '?'):<18} {(label or '-'):<12} {','.join(key_scopes):<16} {status}")


def _report(
    name: str,
    project_id: object,
    api_key: str,
    scopes: list[str],
    label: str | None,
    what: str,
) -> None:
    print(f"project    {name} ({what})")
    print(f"project_id {project_id}")
    print(f"label      {label or '-'}")
    print(f"scopes     {','.join(scopes)}")
    print(f"api_key    {api_key}")
    print("\nShown once. Where it goes depends on the scopes:")
    if "ingest" in scopes:
        print(f"  SDK:       export LLM_METRICS_API_KEY={api_key}")
    if "read" in scopes:
        print(f"  dashboard: LLMOBSERVE_API_KEY={api_key}   (apps/web/.env.local)")


if __name__ == "__main__":
    asyncio.run(main())
