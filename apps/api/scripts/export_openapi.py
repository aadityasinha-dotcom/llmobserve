"""Write the API's OpenAPI document to a file.

    python scripts/export_openapi.py [path]        # default: openapi.json

This is the contract the SDK repo validates its payloads against. It is
generated from the FastAPI app without starting a server or touching the
database, so it can run anywhere the package imports - a CI step, a pre-commit
hook, or a developer's shell after changing a schema.

Deterministic on purpose: keys are sorted and the output ends with a newline,
so a regenerated file diffs cleanly and a CI check can assert it is current.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.main import app


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("openapi.json")
    document = app.openapi()
    target.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    paths = ", ".join(sorted(document.get("paths", {})))
    print(f"wrote {target} ({target.stat().st_size} bytes): {paths}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
