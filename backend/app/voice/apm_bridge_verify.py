"""Backward-compatible import path for the standalone APM verifier."""
from __future__ import annotations

from .apm_verify import main, verify

__all__ = ["main", "verify"]
