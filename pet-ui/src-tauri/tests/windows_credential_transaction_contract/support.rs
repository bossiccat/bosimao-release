use std::collections::HashMap;
use std::fmt::Debug;
use std::sync::{Arc, Mutex};

use jax_pet::credential::{CredentialError, CredentialErrorCode, SecretString};
use jax_pet::credential_windows::{
    CredentialBackend, CredentialSlot, CredentialTransactionLock, LockState,
    TransactionalCredentialStore,
};

pub(super) fn secret(seed: u8) -> SecretString {
    SecretString::parse_utf8(vec![seed; 32]).expect("synthetic credential must be valid")
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum Kind {
    Read,
    Write,
    Delete,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) struct Event(pub(super) Kind, pub(super) CredentialSlot);

#[derive(Clone)]
pub(super) struct MemoryBackend(pub(super) Arc<Mutex<BackendState>>);

#[derive(Default)]
pub(super) struct BackendState {
    pub(super) slots: HashMap<CredentialSlot, Vec<u8>>,
    events: Vec<Event>,
    fail_once: Option<(Kind, CredentialSlot, CredentialErrorCode)>,
    read_after_write_once: Option<(CredentialSlot, Vec<u8>)>,
    read_override_once: Option<(CredentialSlot, Vec<u8>)>,
}

impl MemoryBackend {
    pub(super) fn empty() -> Self {
        Self(Arc::new(Mutex::new(BackendState::default())))
    }

    pub(super) fn seeded(active: Option<u8>, staging: Option<u8>, backup: Option<u8>) -> Self {
        let backend = Self::empty();
        for (slot, value) in [
            (CredentialSlot::Active, active),
            (CredentialSlot::Staging, staging),
            (CredentialSlot::Backup, backup),
        ] {
            if let Some(seed) = value {
                backend.0.lock().unwrap().slots.insert(slot, vec![seed; 32]);
            }
        }
        backend
    }

    pub(super) fn fail_once(&self, kind: Kind, slot: CredentialSlot, code: CredentialErrorCode) {
        self.0.lock().unwrap().fail_once = Some((kind, slot, code));
    }

    pub(super) fn corrupt_next_read_after_write(&self, slot: CredentialSlot, seed: u8) {
        self.0.lock().unwrap().read_after_write_once = Some((slot, vec![seed; 32]));
    }

    pub(super) fn has(&self, slot: CredentialSlot, seed: u8) -> bool {
        self.0
            .lock()
            .unwrap()
            .slots
            .get(&slot)
            .is_some_and(|value| value.iter().all(|byte| *byte == seed))
    }

    pub(super) fn events(&self) -> Vec<Event> {
        self.0.lock().unwrap().events.clone()
    }

    pub(super) fn before(events: &[Event], left: Event, right: Event) -> bool {
        let left = events.iter().position(|event| *event == left);
        let right = events.iter().position(|event| *event == right);
        matches!((left, right), (Some(a), Some(b)) if a < b)
    }

    fn assert_not_leaked<T: Debug>(&self, output: T) {
        let output = format!("{output:?}");
        for value in self.0.lock().unwrap().slots.values() {
            let marker = String::from_utf8_lossy(value);
            assert!(!output.contains(marker.as_ref()));
        }
    }

    fn enter(&self, kind: Kind, slot: CredentialSlot) -> Result<(), CredentialError> {
        let mut state = self.0.lock().unwrap();
        state.events.push(Event(kind, slot));
        if state
            .fail_once
            .is_some_and(|(failed_kind, failed_slot, _)| failed_kind == kind && failed_slot == slot)
        {
            let (_, _, code) = state.fail_once.take().unwrap();
            return Err(CredentialError::new(code));
        }
        Ok(())
    }
}

impl CredentialBackend for MemoryBackend {
    fn read(&self, slot: CredentialSlot) -> Result<Option<SecretString>, CredentialError> {
        self.enter(Kind::Read, slot)?;
        let mut state = self.0.lock().unwrap();
        if state
            .read_override_once
            .as_ref()
            .is_some_and(|(override_slot, _)| *override_slot == slot)
        {
            let (_, value) = state.read_override_once.take().unwrap();
            return SecretString::parse_utf8(value).map(Some);
        }
        state
            .slots
            .get(&slot)
            .cloned()
            .map(SecretString::parse_utf8)
            .transpose()
    }

    fn write(&self, slot: CredentialSlot, value: &SecretString) -> Result<(), CredentialError> {
        self.enter(Kind::Write, slot)?;
        self.assert_not_leaked(("credential write", slot));
        let mut state = self.0.lock().unwrap();
        state.slots.insert(slot, value.expose().as_bytes().to_vec());
        if state
            .read_after_write_once
            .as_ref()
            .is_some_and(|(override_slot, _)| *override_slot == slot)
        {
            state.read_override_once = state.read_after_write_once.take();
        }
        Ok(())
    }

    fn delete(&self, slot: CredentialSlot) -> Result<(), CredentialError> {
        self.enter(Kind::Delete, slot)?;
        self.0.lock().unwrap().slots.remove(&slot);
        Ok(())
    }
}

#[derive(Clone)]
pub(super) struct FakeLock(Arc<Mutex<(LockState, usize)>>);

impl FakeLock {
    pub(super) fn new(state: LockState) -> Self {
        Self(Arc::new(Mutex::new((state, 0))))
    }

    pub(super) fn acquisitions(&self) -> usize {
        self.0.lock().unwrap().1
    }
}

pub(super) struct FakeGuard;

impl CredentialTransactionLock for FakeLock {
    type Guard = FakeGuard;

    fn acquire(&self) -> Result<(Self::Guard, LockState), CredentialError> {
        let mut state = self.0.lock().unwrap();
        state.1 += 1;
        match state.0 {
            LockState::Busy => Err(CredentialError::new(CredentialErrorCode::CredentialBusy)),
            disposition => Ok((FakeGuard, disposition)),
        }
    }
}

pub(super) fn store(
    backend: MemoryBackend,
    lock: FakeLock,
) -> TransactionalCredentialStore<MemoryBackend, FakeLock> {
    TransactionalCredentialStore::new(backend, lock)
}
