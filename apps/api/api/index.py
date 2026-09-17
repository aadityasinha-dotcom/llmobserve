"""Vercel entrypoint for the API.

Vercel's Python runtime serves an ASGI app exported as `app` from a file under
`api/`. `vercel.json` rewrites every path to this function, so FastAPI still sees
the original request path and routes it as it would under uvicorn.

Nothing belongs here but the import. Configuration comes from environment
variables set on the Vercel project; see docs/deploying-to-vercel.md.
"""

import sys
from pathlib import Path

# The project root (apps/api) holds the `app` package. Put it on the path
# explicitly rather than relying on the runtime's working directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app

__all__ = ["app"]
