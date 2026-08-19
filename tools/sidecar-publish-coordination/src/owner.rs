use serde_json::Value;
use std::fs::OpenOptions;
use std::io::Write;
use std::path::Path;

#[derive(Debug, PartialEq, Eq)]
pub enum OwnerState {
    Live,
    Absent,
    PidReused,
    Invalid,
    IdentityUnavailable,
}

#[derive(Debug, PartialEq, Eq)]
pub enum ProcessIdentity {
    Absent,
    Verified {
        creation_time: String,
        identity: String,
    },
    Unavailable {
        reason: String,
    },
}

#[derive(Debug, PartialEq, Eq)]
pub struct Owner {
    pub schema_version: u32,
    pub token: String,
    pub pid: u32,
    pub created_at: String,
    pub process_creation_time: String,
    pub process_creation_identity: String,
}

pub fn create_owner_file(path: &Path, bytes: &[u8]) -> Result<(), String> {
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|error| error.to_string())?;
    file.write_all(bytes).map_err(|error| error.to_string())?;
    file.sync_all().map_err(|error| error.to_string())?;
    Ok(())
}

pub fn classify_owner_bytes<F>(bytes: &[u8], resolve_identity: F) -> OwnerState
where
    F: FnOnce(u32) -> ProcessIdentity,
{
    let owner = match parse_owner_bytes(bytes) {
        Ok(owner) => owner,
        Err(_) => return OwnerState::Invalid,
    };
    match resolve_identity(owner.pid) {
        ProcessIdentity::Absent => OwnerState::Absent,
        ProcessIdentity::Unavailable { .. } => OwnerState::IdentityUnavailable,
        ProcessIdentity::Verified {
            creation_time,
            identity,
        } => {
            if creation_time == owner.process_creation_time
                && identity == owner.process_creation_identity
            {
                OwnerState::Live
            } else {
                OwnerState::PidReused
            }
        }
    }
}

pub fn parse_owner_bytes(bytes: &[u8]) -> Result<Owner, String> {
    let value: Value = serde_json::from_slice(bytes).map_err(|error| error.to_string())?;
    let object = value
        .as_object()
        .ok_or_else(|| "owner must be an object".to_owned())?;
    let expected = [
        "schema_version",
        "token",
        "pid",
        "created_at",
        "process_creation_time",
        "process_creation_identity",
    ];
    if object.len() != expected.len() || expected.iter().any(|key| !object.contains_key(*key)) {
        return Err("owner contains unexpected or missing fields".to_owned());
    }
    let schema_version = object
        .get("schema_version")
        .and_then(Value::as_u64)
        .ok_or_else(|| "schema_version must be an integer".to_owned())?;
    let pid = object
        .get("pid")
        .and_then(Value::as_u64)
        .ok_or_else(|| "pid must be an integer".to_owned())?;
    if schema_version != 1 || pid == 0 || schema_version > u32::MAX as u64 || pid > u32::MAX as u64 {
        return Err("owner integer fields are out of range".to_owned());
    }
    let token = required_string(object, "token")?;
    let created_at = required_string(object, "created_at")?;
    let process_creation_time = required_string(object, "process_creation_time")?;
    let process_creation_identity = required_string(object, "process_creation_identity")?;
    if !is_uuid(&token)
        || !is_rfc3339_utc(&created_at)
        || !is_rfc3339_utc(&process_creation_time)
    {
        return Err("owner UUID or timestamp is invalid".to_owned());
    }
    Ok(Owner {
        schema_version: schema_version as u32,
        token,
        pid: pid as u32,
        created_at,
        process_creation_time,
        process_creation_identity,
    })
}

fn required_string(object: &serde_json::Map<String, Value>, key: &str) -> Result<String, String> {
    let value = object
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("{key} must be a string"))?;
    if value.is_empty() {
        return Err(format!("{key} must not be empty"));
    }
    Ok(value.to_owned())
}

fn is_uuid(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() == 36
        && [8, 13, 18, 23].iter().all(|index| bytes[*index] == b'-')
        && bytes.iter().enumerate().all(|(index, byte)| {
            [8, 13, 18, 23].contains(&index) || byte.is_ascii_hexdigit()
        })
}

fn is_rfc3339_utc(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() >= 20
        && bytes[4] == b'-'
        && bytes[7] == b'-'
        && bytes[10] == b'T'
        && bytes[13] == b':'
        && bytes[16] == b':'
        && bytes.last() == Some(&b'Z')
        && bytes.iter().enumerate().all(|(index, byte)| {
            [4, 7, 10, 13, 16, bytes.len() - 1].contains(&index)
                || byte.is_ascii_digit()
                || (index >= 19 && *byte == b'.')
        })
}
