use sidecar_publish_coordination::coordinator::{AcquireError, Coordinator};
use sidecar_publish_coordination::protocol::{parse_request, Request};
use serde_json::json;
use std::io::{self, BufRead};
use std::path::PathBuf;

fn main() {
    let mut coordinator: Option<Coordinator> = None;

    for line in io::stdin().lock().lines() {
        let line = match line {
            Ok(line) => line,
            Err(error) => {
                eprintln!("sidecar-publish-coordination: stdin: {error}");
                std::process::exit(2);
            }
        };

        let request = match parse_request(&line) {
            Ok(request) => request,
            Err(error) => {
                respond(
                    "protocol",
                    false,
                    "invalid_request",
                    0,
                    &error.to_string(),
                );
                continue;
            }
        };

        match request {
            Request::Acquire {
                runtime_root,
                owner,
                timeout_ms,
            } => {
                if coordinator.is_some() {
                    respond(
                        "acquire",
                        false,
                        "busy",
                        0,
                        "a lease is already held by this process",
                    );
                    continue;
                }
                let root = PathBuf::from(&runtime_root);
                let built = Coordinator::new(&root);
                let built = match built {
                    Ok(built) => built,
                    Err(error) => {
                        respond("acquire", false, "lock_open_failed", 0, &error);
                        continue;
                    }
                };
                let mut instance = built;
                let owner_json = serde_json::to_string(&owner_record(&owner)).unwrap();
                match instance.acquire(owner_json) {
                    Ok(lease) => {
                        coordinator = Some(instance);
                        println!(
                            "{}",
                            json!({
                                "operation": "acquire",
                                "success": true,
                                "status": "acquired",
                                "lease_id": lease.lease_id,
                                "token": lease.token,
                                "native_error_code": 0,
                                "diagnostic": "lease held under named mutex"
                            })
                        );
                    }
                    Err(error) => {
                        let status = acquire_status(&error);
                        respond(
                            "acquire",
                            false,
                            status,
                            0,
                            &error.to_string(),
                        );
                    }
                }
                let _ = timeout_ms; // bounded wait lives inside acquire (5s);
                                    // protocol field kept for forward compat.
            }
            Request::Publish {
                lease_id,
                temporary_path,
                current_path,
            } => match coordinator.as_mut() {
                Some(instance) => {
                    match instance.publish(
                        &lease_id,
                        PathBuf::from(temporary_path),
                        PathBuf::from(current_path),
                    ) {
                        Ok(()) => respond(
                            "publish",
                            true,
                            "committed",
                            0,
                            "pointer atomically replaced under mutex",
                        ),
                        Err(message) => respond("publish", false, "commit_failed", 0, &message),
                    };
                }
                None => respond(
                    "publish",
                    false,
                    "no_active_lease",
                    0,
                    "acquire must succeed before publish",
                ),
            },
            Request::Release {
                lease_id,
                expected_token,
            } => match coordinator.as_mut() {
                Some(instance) => {
                    let fake_lease = sidecar_publish_coordination::coordinator::Lease {
                        lease_id: lease_id.clone(),
                        token: expected_token.clone(),
                        runtime_root: PathBuf::new(),
                    };
                    let outcome = instance.release(&fake_lease, &expected_token);
                    match outcome {
                        Ok(sidecar_publish_coordination::coordinator::ReleaseOutcome::Ok) => {
                            coordinator = None;
                            respond(
                                "release",
                                true,
                                "released",
                                0,
                                "owner removed and mutex released",
                            );
                        }
                        Ok(sidecar_publish_coordination::coordinator::ReleaseOutcome::OwnerMismatch) => {
                            respond(
                                "release",
                                false,
                                "owner_mismatch",
                                0,
                                "token does not match the held lease; owner kept",
                            );
                        }
                        Ok(sidecar_publish_coordination::coordinator::ReleaseOutcome::LeaseLost) => {
                            coordinator = None;
                            respond(
                                "release",
                                false,
                                "no_active_lease",
                                0,
                                "no lease is held by this process",
                            );
                        }
                        Err(message) => {
                            respond("release", false, "release_failed", 0, &message);
                        }
                    }
                }
                None => respond(
                    "release",
                    false,
                    "no_active_lease",
                    0,
                    "acquire must succeed before release",
                ),
            },
        }
    }
}

fn owner_record(owner: &sidecar_publish_coordination::protocol::Owner) -> serde_json::Value {
    json!({
        "schema_version": owner.schema_version,
        "token": owner.token,
        "pid": owner.pid,
        "created_at": owner.created_at,
        "process_creation_time": owner.process_creation_time,
        "process_creation_identity": owner.process_creation_identity,
    })
}

fn acquire_status(error: &AcquireError) -> &'static str {
    match error {
        AcquireError::Busy => "busy",
        AcquireError::LockPoisoned => "lock_poisoned",
        AcquireError::OwnerInvalid => "owner_invalid",
        AcquireError::IdentityUnavailable => "identity_unavailable",
        AcquireError::Io(_) => "io_error",
    }
}

fn respond(
    operation: &str,
    success: bool,
    status: &str,
    native_error_code: u32,
    diagnostic: &str,
) {
    println!(
        "{}",
        json!({
            "operation": operation,
            "success": success,
            "status": status,
            "native_error_code": native_error_code,
            "diagnostic": diagnostic
        })
    );
}
