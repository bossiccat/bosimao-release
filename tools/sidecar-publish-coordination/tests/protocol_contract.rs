use sidecar_publish_coordination::protocol::{parse_request, ProtocolError, Request};

fn owner() -> &'static str {
    r#"{"schema_version":1,"token":"550e8400-e29b-41d4-a716-446655440000","pid":1234,"created_at":"2026-08-18T12:00:00.000Z","process_creation_time":"2026-08-18T12:00:00.000Z","process_creation_identity":"sha256-owner"}"#
}

#[test]
fn acquire_requires_exact_request_schema_and_rfc3339_owner_times() {
    let request = format!(
        r#"{{"operation":"acquire","runtime_root":"C:\\runtime","owner":{},"timeout_ms":2000}}"#,
        owner()
    );

    let parsed = parse_request(&request).expect("acquire request should parse");
    assert!(matches!(parsed, Request::Acquire { timeout_ms: 2000, .. }));

    let extra = request.replacen("\"timeout_ms\":2000", "\"timeout_ms\":2000,\"unexpected\":true", 1);
    assert!(matches!(parse_request(&extra), Err(ProtocolError::InvalidRequest(_))));

    let non_rfc3339 = request.replace("2026-08-18T12:00:00.000Z", "08/18/2026 12:00:00");
    assert!(matches!(parse_request(&non_rfc3339), Err(ProtocolError::InvalidOwner(_))));
}

#[test]
fn publish_and_release_reject_unknown_fields_and_empty_lease_ids() {
    let publish = r#"{"operation":"publish","lease_id":"lease-1","temporary_path":"C:\\runtime\\next.tmp","current_path":"C:\\runtime\\current.json"}"#;
    assert!(matches!(parse_request(publish), Ok(Request::Publish { .. })));
    assert!(matches!(parse_request(r#"{"operation":"publish","lease_id":"","temporary_path":"C:\\runtime\\next.tmp","current_path":"C:\\runtime\\current.json"}"#), Err(ProtocolError::InvalidRequest(_))));
    assert!(matches!(parse_request(r#"{"operation":"release","lease_id":"lease-1","expected_token":"550e8400-e29b-41d4-a716-446655440000","extra":true}"#), Err(ProtocolError::InvalidRequest(_))));
}
