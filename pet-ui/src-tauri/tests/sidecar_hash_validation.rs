mod support;

use std::time::Duration;

use jax_pet::sidecar::{SidecarError, SidecarSpec, SidecarSupervisor};

fn supervisor(expected_sha256: &str) -> SidecarSupervisor {
    let fixture = support::sidecar_fixture();
    SidecarSupervisor::new(SidecarSpec {
        binary_path: fixture.binary_path,
        expected_sha256: expected_sha256.to_string(),
        integrity: fixture.integrity,
        args: vec![],
        ca_cert_path: std::path::PathBuf::from("certs/ca.crt"),
        graceful_timeout: Duration::from_secs(1),
        kill_timeout: Duration::from_secs(1),
    })
}

#[test]
fn missing_expected_hash_fails_closed() {
    let error = supervisor("")
        .validate_binary()
        .expect_err("empty hash must fail");
    assert!(matches!(error, SidecarError::ExpectedHashMissing));
}

#[test]
fn malformed_expected_hash_fails_closed() {
    for invalid in ["a", &"A".repeat(64), &"g".repeat(64)] {
        let error = supervisor(invalid)
            .validate_binary()
            .expect_err("invalid hash must fail");
        assert!(matches!(error, SidecarError::ExpectedHashInvalid(_)));
    }
}
