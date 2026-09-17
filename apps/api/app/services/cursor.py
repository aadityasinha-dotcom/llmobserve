"""Opaque keyset cursors for the trace list.

Offset pagination is wrong for this table: `LIMIT 50 OFFSET 10000` makes Postgres
walk and discard 10,000 rows, and any trace written while the client pages shifts
every subsequent offset, so rows are silently duplicated or skipped. A keyset
cursor seeks straight to its position and is unaffected by writes ahead of it.

The cursor encodes the sort key of the last row returned - `(started_at, id)`,
the exact pair the ORDER BY uses. Both halves are required. started_at alone is
not a unique key: one ingest batch can write hundreds of traces sharing a
millisecond, and a cursor that could not tell them apart would drop or repeat
whichever ones fell across the page boundary.

Base64 is encoding, not protection. The values inside are a timestamp and an id
the caller already has, so there is nothing to hide - it exists to stop clients
from parsing the format and depending on it. Nothing here is trusted: a cursor
is fully validated on the way back in, and a tampered one is a 400, never a
query built from attacker-chosen text.
"""

import base64
import binascii
from datetime import UTC, datetime
from uuid import UUID

# Version prefix. If the sort key ever changes, old cursors must be rejected
# outright rather than silently reinterpreted against a different ordering.
_PREFIX = "v1"
_SEPARATOR = "|"


class InvalidCursorError(ValueError):
    """Raised when a cursor is malformed, truncated, or of an unknown version."""


def encode_cursor(started_at: datetime, trace_id: UUID) -> str:
    """Encode one row's sort key into an opaque cursor."""
    raw = f"{_PREFIX}{_SEPARATOR}{started_at.isoformat()}{_SEPARATOR}{trace_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Decode a cursor back into its (started_at, id) sort key.

    Raises InvalidCursorError for anything that is not a cursor this server
    issued. The caller turns that into a 400: a bad cursor is a client error,
    and answering it with an unfiltered first page would silently restart
    pagination instead of reporting the fault.
    """
    # urlsafe_b64decode requires the padding that encode_cursor stripped.
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidCursorError("Cursor is not valid base64url.") from exc

    parts = raw.split(_SEPARATOR)
    if len(parts) != 3:
        raise InvalidCursorError("Cursor is malformed.")

    version, timestamp_text, id_text = parts
    if version != _PREFIX:
        raise InvalidCursorError(f"Unsupported cursor version {version!r}.")

    try:
        started_at = datetime.fromisoformat(timestamp_text)
    except ValueError as exc:
        raise InvalidCursorError("Cursor carries an unparseable timestamp.") from exc

    try:
        trace_id = UUID(id_text)
    except ValueError as exc:
        raise InvalidCursorError("Cursor carries an unparseable trace id.") from exc

    # A naive timestamp would compare against a timestamptz column under the
    # session TimeZone rather than as UTC, which silently shifts the seek.
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)

    return started_at, trace_id
