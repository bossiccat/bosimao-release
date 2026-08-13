"""Sidecar current/next credential hash rotation contract tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.voice.auth import AuthError, CredentialValidator
from app.voice.config import (
    SidecarCredentialConfigError,
    SidecarCredentialHashSet,
    build_sidecar_credential_hashes,
)
from app.voice.storage import VoiceStore

DEVICE_A = "dev-a-000000000000000000000001"
DEVICE_B = "dev-b-000000000000000000000002"
SECRET_A = "secret-a-0123456789abcdef01234567"
SECRET_B = "secret-b-0123456789abcdef01234567"
SIDECAR_SECRET = "sidecar-secret-0123456789abcdef"
SIDECAR_NEXT_SECRET = "sidecar-next-secret-0123456789ab"
UTC = timezone.utc


def _rotation_hashes(
    enabled_at: datetime,
    expires_at: datetime,
) -> SidecarCredentialHashSet:
    return build_sidecar_credential_hashes(
        current_secret=SIDECAR_SECRET,
        next_secret=SIDECAR_NEXT_SECRET,
        next_enabled_at=enabled_at.isoformat().replace("+00:00", "Z"),
        next_expires_at=expires_at.isoformat().replace("+00:00", "Z"),
        config_revision="deploy-r2",
    )


def _store(tmp_path: Path) -> VoiceStore:
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    store.save_device(DEVICE_A, SECRET_A, device_name="phone-a")
    store.save_device(DEVICE_B, SECRET_B, device_name="phone-b")
    return store


def test_build_sidecar_hashes_accepts_only_current() -> None:
    credentials = build_sidecar_credential_hashes(current_secret=SIDECAR_SECRET)
    assert credentials.current_hash.startswith("jax-static-v1$")
    assert credentials.next_hash is None
    assert credentials.rotation_state(datetime(2026, 8, 9, tzinfo=UTC)) == "rotation_inactive"


@pytest.mark.parametrize(
    "overrides",
    [
        {"current_secret": ""},
        {"next_secret": SIDECAR_NEXT_SECRET},
        {"next_secret": SIDECAR_NEXT_SECRET, "next_enabled_at": "2026-08-09T00:00:00Z"},
        {
            "next_secret": SIDECAR_NEXT_SECRET,
            "next_enabled_at": "2026-08-09T00:00:00",
            "next_expires_at": "2026-08-09T00:10:00Z",
        },
        {
            "next_secret": SIDECAR_NEXT_SECRET,
            "next_enabled_at": "2026-08-09T00:10:00Z",
            "next_expires_at": "2026-08-09T00:00:00Z",
        },
        {
            "next_secret": SIDECAR_NEXT_SECRET,
            "next_enabled_at": "2026-08-09T00:00:00Z",
            "next_expires_at": "2026-08-09T00:10:01Z",
        },
        {
            "next_secret": SIDECAR_SECRET,
            "next_enabled_at": "2026-08-09T00:00:00Z",
            "next_expires_at": "2026-08-09T00:10:00Z",
        },
    ],
)
def test_build_sidecar_hashes_rejects_invalid_configuration(overrides: dict[str, str]) -> None:
    values = {"current_secret": SIDECAR_SECRET, **overrides}
    with pytest.raises(SidecarCredentialConfigError):
        build_sidecar_credential_hashes(**values)


def test_sidecar_rotation_boundaries_and_same_principal(tmp_path: Path) -> None:
    enabled = datetime(2026, 8, 9, 1, 0, tzinfo=UTC)
    credentials = _rotation_hashes(enabled, enabled + timedelta(minutes=10))
    store = _store(tmp_path)

    scheduled = CredentialValidator(store, "", credentials, clock=lambda: enabled - timedelta(seconds=1))
    assert scheduled.verify_sidecar(SIDECAR_SECRET).type == "sidecar"
    with pytest.raises(AuthError) as before:
        scheduled.verify_sidecar(SIDECAR_NEXT_SECRET)
    assert before.value.code == 40101

    at_enabled = CredentialValidator(store, "", credentials, clock=lambda: enabled)
    assert at_enabled.verify_sidecar(SIDECAR_SECRET) == at_enabled.verify_sidecar(SIDECAR_NEXT_SECRET)

    at_expiry = CredentialValidator(
        store, "", credentials, clock=lambda: enabled + timedelta(minutes=10)
    )
    assert at_expiry.verify_sidecar(SIDECAR_SECRET).credential_id == "sidecar-credential"
    with pytest.raises(AuthError) as expired:
        at_expiry.verify_sidecar(SIDECAR_NEXT_SECRET)
    assert expired.value.code == 40101


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 8, 9, 0, 59, 59, tzinfo=UTC),
        datetime(2026, 8, 9, 1, 10, tzinfo=UTC),
    ],
)
def test_inactive_rotation_states_compare_only_current(
    tmp_path: Path, monkeypatch, now: datetime
) -> None:
    enabled = datetime(2026, 8, 9, 1, 0, tzinfo=UTC)
    credentials = _rotation_hashes(enabled, enabled + timedelta(minutes=10))
    calls: list[tuple[str, str]] = []
    real_compare = __import__("hmac").compare_digest

    def spy(left: str, right: str) -> bool:
        calls.append((left, right))
        return real_compare(left, right)

    monkeypatch.setattr("app.voice.auth.hmac.compare_digest", spy)
    CredentialValidator(_store(tmp_path), "", credentials, clock=lambda: now).verify_sidecar(
        SIDECAR_SECRET
    )
    assert len(calls) == 1
    assert calls[0][1] == credentials.current_hash.split("$", 1)[1]


def test_active_rotation_always_compares_current_then_next(tmp_path: Path, monkeypatch) -> None:
    enabled = datetime(2026, 8, 9, 1, 0, tzinfo=UTC)
    credentials = _rotation_hashes(enabled, enabled + timedelta(minutes=10))
    calls: list[tuple[str, str]] = []
    real_compare = __import__("hmac").compare_digest

    def spy(left: str, right: str) -> bool:
        calls.append((left, right))
        return real_compare(left, right)

    monkeypatch.setattr("app.voice.auth.hmac.compare_digest", spy)
    active = CredentialValidator(_store(tmp_path), "", credentials, clock=lambda: enabled)
    active.verify_sidecar(SIDECAR_SECRET)
    assert [right for _, right in calls] == [
        credentials.current_hash.split("$", 1)[1],
        credentials.next_hash.split("$", 1)[1],
    ]


def test_malicious_sidecar_hash_fails_closed_before_compare(tmp_path: Path, monkeypatch) -> None:
    invalid = SidecarCredentialHashSet(current_hash="not-a-static-hash")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "app.voice.auth.hmac.compare_digest",
        lambda left, right: calls.append((left, right)) or False,
    )
    validator = CredentialValidator(_store(tmp_path), "", invalid)
    with pytest.raises(AuthError) as error:
        validator.verify_sidecar(SIDECAR_SECRET)
    assert error.value.code == 50300
    assert calls == []


def test_non_utc_clock_fails_closed_before_compare(tmp_path: Path, monkeypatch) -> None:
    enabled = datetime(2026, 8, 9, 1, 0, tzinfo=UTC)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "app.voice.auth.hmac.compare_digest",
        lambda left, right: calls.append((left, right)) or False,
    )
    validator = CredentialValidator(
        _store(tmp_path),
        "",
        _rotation_hashes(enabled, enabled + timedelta(minutes=10)),
        clock=lambda: datetime(2026, 8, 9, 1, 0),
    )
    with pytest.raises(AuthError) as error:
        validator.verify_sidecar(SIDECAR_SECRET)
    assert error.value.code == 50300
    assert calls == []
