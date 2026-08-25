"""ISO8601 serialization helpers for HTTP-facing timestamps.

OpenAPI (docs/api/commercial-voice-openapi.yaml) declares date-time strings for
expires_at / terminal_at / hello expiry. Storage keeps epoch floats; these
helpers convert only at serialization or normalization boundaries, following
the hello_service.py pattern.

Never import business modules here: pure functions only.
"""
from __future__ import annotations

from datetime import datetime, timezone


def epoch_to_iso8601(value: float | int) -> str:
    """Serialize an epoch timestamp as an RFC3339 UTC string ("...Z")."""
    return (
        datetime.fromtimestamp(float(value), tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def iso8601_to_epoch(text: str) -> float:
    """Parse an RFC3339/ISO8601 string (optionally "Z"-suffixed) to epoch.

    Raises ValueError on non-numeric input that is not a valid timestamp, so
    callers fail loudly instead of silently storing garbage.
    """
    normalized = text.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def as_epoch(value: float | int | str) -> float:
    """Normalize a float-or-ISO8601 timestamp into an epoch float."""
    if isinstance(value, str):
        return iso8601_to_epoch(value)
    return float(value)
