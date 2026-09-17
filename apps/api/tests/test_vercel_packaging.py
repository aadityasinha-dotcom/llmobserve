"""Guards for the Vercel deployment's packaging.

Vercel installs from requirements.txt while every other environment installs
from pyproject.toml. Two dependency lists will drift unless something notices;
this is the something.
"""

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Present in pyproject, deliberately absent from the function bundle.
NOT_NEEDED_AT_RUNTIME = {"uvicorn", "alembic"}


def _name(requirement: str) -> str:
    return re.split(r"[\s\[<>=!~;]", requirement.strip(), maxsplit=1)[0].lower()


def _requirements() -> set[str]:
    lines = (ROOT / "requirements.txt").read_text().splitlines()
    return {_name(line) for line in lines if line.strip() and not line.lstrip().startswith("#")}


def _pyproject() -> set[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    return {_name(dep) for dep in data["project"]["dependencies"]}


def test_requirements_cover_every_runtime_dependency() -> None:
    missing = _pyproject() - NOT_NEEDED_AT_RUNTIME - _requirements()
    assert not missing, f"add to requirements.txt: {sorted(missing)}"


def test_requirements_add_nothing_pyproject_lacks() -> None:
    extra = _requirements() - _pyproject()
    assert not extra, f"add to pyproject.toml or remove: {sorted(extra)}"


def test_vercel_config_declares_no_rewrites() -> None:
    """A rewrite here is what broke the first deployment.

    Vercel detects the FastAPI entrypoint and routes every path to it. Adding
    `rewrites: [{source: "/(.*)", destination: "/api/index"}]` replaces the path
    the ASGI app receives, so FastAPI sees "/api/index" for every request and
    answers 404 to all of them - including /healthz and /openapi.json, which
    makes it look like the app failed to load rather than like a routing bug.
    """
    config = json.loads((ROOT / "vercel.json").read_text())
    assert "rewrites" not in config
    assert "routes" not in config


def test_function_config_keys_the_real_entrypoint() -> None:
    """`functions` is keyed by the resolved entrypoint file, which must exist.

    app/main.py is one of the filenames Vercel auto-detects (`main.py` inside
    `app/`). A key that does not resolve is silently ignored, taking maxDuration
    and excludeFiles with it.
    """
    config = json.loads((ROOT / "vercel.json").read_text())
    (key,) = config["functions"].keys()
    assert key == "app/main.py"
    assert (ROOT / key).is_file()


def test_entrypoint_exports_a_fastapi_app_named_app() -> None:
    """Vercel looks for a FastAPI instance named exactly `app`."""
    from fastapi import FastAPI

    from app.main import app

    assert isinstance(app, FastAPI)


def test_env_files_are_never_bundled() -> None:
    ignored = (ROOT / ".vercelignore").read_text().splitlines()
    assert ".env" in ignored
    assert ".venv" in ignored
