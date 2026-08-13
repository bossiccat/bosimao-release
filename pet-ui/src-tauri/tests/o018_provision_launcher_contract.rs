//! O-018 切片 2：受管 launcher 的 CSPRNG 与无泄漏静态契约。

#[test]
fn launcher_uses_windows_csprng_and_fixed_local_helper_only() {
    let source = include_str!("../src/bin/provision_sidecar_credential_launcher.rs");
    for required in [
        "BCryptGenRandom",
        "BCRYPT_USE_SYSTEM_PREFERRED_RNG",
        "SecretString::parse_utf8",
        "WindowsProvisionTransport",
    ] {
        assert!(
            source.contains(required),
            "missing launcher primitive {required}"
        );
    }
    for forbidden in [
        "std::env::set_var",
        "Command::env",
        "Command::arg",
        "std::fs::write",
        "println!",
        "eprintln!",
        "provision_sidecar_credential.exe",
        "current_exe",
    ] {
        assert!(
            !source.contains(forbidden),
            "forbidden launcher API {forbidden}"
        );
    }
}

#[test]
fn launcher_secret_is_independent_and_valid_without_embedding_a_literal() {
    let source = include_str!("../src/bin/provision_sidecar_credential_launcher.rs");
    assert!(source.contains("const SECRET_BYTES: usize = 32"));
    assert!(source.contains("hex_encode"));
    assert!(source.contains("ExitCode::from"));
    assert!(!source.contains("VOICE_SIDECAR_CREDENTIAL"));
}
