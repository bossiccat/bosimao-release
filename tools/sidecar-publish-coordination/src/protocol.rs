use serde::Deserialize;
use serde_json::Value;
use std::fmt;

#[derive(Debug, PartialEq, Eq)]
pub enum Request {
    Acquire {
        runtime_root: String,
        owner: Owner,
        timeout_ms: u32,
    },
    Publish {
        lease_id: String,
        temporary_path: String,
        current_path: String,
    },
    Release {
        lease_id: String,
        expected_token: String,
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

#[derive(Debug, PartialEq, Eq)]
pub enum ProtocolError {
    InvalidJson(String),
    InvalidRequest(String),
    InvalidOwner(String),
}

impl fmt::Display for ProtocolError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidJson(message) => write!(formatter, "invalid JSON: {message}"),
            Self::InvalidRequest(message) => write!(formatter, "invalid request: {message}"),
            Self::InvalidOwner(message) => write!(formatter, "invalid owner: {message}"),
        }
    }
}

impl std::error::Error for ProtocolError {}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct AcquireRequest {
    operation: String,
    runtime_root: String,
    owner: OwnerRequest,
    timeout_ms: u32,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PublishRequest {
    operation: String,
    lease_id: String,
    temporary_path: String,
    current_path: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ReleaseRequest {
    operation: String,
    lease_id: String,
    expected_token: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct OwnerRequest {
    schema_version: u32,
    token: String,
    pid: u32,
    created_at: String,
    process_creation_time: String,
    process_creation_identity: String,
}

pub fn parse_request(input: &str) -> Result<Request, ProtocolError> {
    let value: Value = serde_json::from_str(input)
        .map_err(|error| ProtocolError::InvalidJson(error.to_string()))?;
    let operation = value
        .get("operation")
        .and_then(Value::as_str)
        .ok_or_else(|| ProtocolError::InvalidRequest("operation is required".to_owned()))?;

    match operation {
        "acquire" => {
            let request: AcquireRequest = serde_json::from_value(value)
                .map_err(|error| ProtocolError::InvalidRequest(error.to_string()))?;
            if request.operation != "acquire" || request.runtime_root.is_empty() {
                return Err(ProtocolError::InvalidRequest(
                    "acquire requires a non-empty runtime_root".to_owned(),
                ));
            }
            let owner = validate_owner(request.owner)?;
            Ok(Request::Acquire {
                runtime_root: request.runtime_root,
                owner,
                timeout_ms: request.timeout_ms,
            })
        }
        "publish" => {
            let request: PublishRequest = serde_json::from_value(value)
                .map_err(|error| ProtocolError::InvalidRequest(error.to_string()))?;
            if request.operation != "publish"
                || request.lease_id.is_empty()
                || request.temporary_path.is_empty()
                || request.current_path.is_empty()
            {
                return Err(ProtocolError::InvalidRequest(
                    "publish requires non-empty lease_id and paths".to_owned(),
                ));
            }
            Ok(Request::Publish {
                lease_id: request.lease_id,
                temporary_path: request.temporary_path,
                current_path: request.current_path,
            })
        }
        "release" => {
            let request: ReleaseRequest = serde_json::from_value(value)
                .map_err(|error| ProtocolError::InvalidRequest(error.to_string()))?;
            if request.operation != "release"
                || request.lease_id.is_empty()
                || !is_uuid(&request.expected_token)
            {
                return Err(ProtocolError::InvalidRequest(
                    "release requires a non-empty lease_id and UUID token".to_owned(),
                ));
            }
            Ok(Request::Release {
                lease_id: request.lease_id,
                expected_token: request.expected_token,
            })
        }
        _ => Err(ProtocolError::InvalidRequest("unknown operation".to_owned())),
    }
}

fn validate_owner(owner: OwnerRequest) -> Result<Owner, ProtocolError> {
    if owner.schema_version != 1
        || !is_uuid(&owner.token)
        || owner.pid == 0
        || !is_rfc3339_utc(&owner.created_at)
        || !is_rfc3339_utc(&owner.process_creation_time)
        || owner.process_creation_identity.is_empty()
    {
        return Err(ProtocolError::InvalidOwner(
            "owner schema or identity fields are invalid".to_owned(),
        ));
    }
    Ok(Owner {
        schema_version: owner.schema_version,
        token: owner.token,
        pid: owner.pid,
        created_at: owner.created_at,
        process_creation_time: owner.process_creation_time,
        process_creation_identity: owner.process_creation_identity,
    })
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
