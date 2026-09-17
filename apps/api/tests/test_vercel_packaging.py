"""Guards for the Vercel deployment's packaging.

Vercel installs from requirements.txt while every other environment installs
from pyproject.toml. Two dependency lists will drift unless something notices;
this is the something.
"""

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


def test_entrypoint_exports_the_application() -> None:
    """Import it the way the runtime does: by file, with no package context."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("vercel_entry", ROOT / "api" / "index.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    from app.main import app

    assert module.app is app


def test_env_files_are_never_bundled() -> None:
    ignored = (ROOT / ".vercelignore").read_text().splitlines()
    assert ".env" in ignored
    assert ".venv" in ignored
