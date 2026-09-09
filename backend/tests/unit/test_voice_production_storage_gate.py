from __future__ import annotations

import pytest

from app.voice.config import ProductionGateError, validate_voice_storage


def test_production_rejects_sqlite_storage_backend() -> None:
    with pytest.raises(ProductionGateError, match="postgres"):
        validate_voice_storage(
            production=True,
            storage_backend="sqlite",
            database_url="sqlite:///backend/data/voice.db",
        )


def test_production_rejects_missing_postgres_dsn() -> None:
    with pytest.raises(ProductionGateError, match="database"):
        validate_voice_storage(
            production=True,
            storage_backend="postgresql",
            database_url="",
        )


def test_production_accepts_postgres_dsn() -> None:
    assert validate_voice_storage(
        production=True,
        storage_backend="postgresql",
        database_url="postgresql://voice-api@private.example:5432/voice",
    ) == []


def test_development_can_use_sqlite_fixture() -> None:
    assert validate_voice_storage(
        production=False,
        storage_backend="sqlite",
        database_url="sqlite:///tmp/voice.db",
    ) == []
