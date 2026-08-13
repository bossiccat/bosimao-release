use crate::credential::{CredentialError, CredentialProvider, SecretString};
use crate::sidecar::{SidecarError, SidecarSupervisor, ValidatedSpawnError};

pub struct LaunchCredential {
    credential: SecretString,
}

impl LaunchCredential {
    pub fn new(credential: SecretString) -> Self {
        Self { credential }
    }
    pub(crate) fn expose(&self) -> &str {
        self.credential.expose()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RunPolicy {
    pub has_run_successfully: bool,
    pub restart_allowed: bool,
    pub revoked: bool,
}

impl Default for RunPolicy {
    fn default() -> Self {
        Self {
            has_run_successfully: false,
            restart_allowed: false,
            revoked: false,
        }
    }
}

#[derive(Debug)]
pub enum SidecarLaunchError {
    Credential(CredentialError),
    Sidecar(SidecarError),
    RestartNotAllowed,
}

fn map_validated_spawn_error(error: ValidatedSpawnError<CredentialError>) -> SidecarLaunchError {
    match error {
        ValidatedSpawnError::Validation(error) | ValidatedSpawnError::Spawn(error) => {
            SidecarLaunchError::Sidecar(error)
        }
        ValidatedSpawnError::Load(error) => SidecarLaunchError::Credential(error),
    }
}

pub struct SidecarCredentialService<C: CredentialProvider> {
    credential_provider: C,
    run_policy: RunPolicy,
}

impl<C: CredentialProvider> SidecarCredentialService<C> {
    pub fn new(credential_provider: C) -> Self {
        Self {
            credential_provider,
            run_policy: RunPolicy::default(),
        }
    }

    pub fn run_policy(&self) -> RunPolicy {
        self.run_policy
    }

    pub fn prepare_launch(&self) -> Result<LaunchCredential, CredentialError> {
        self.credential_provider
            .load_active()
            .map(LaunchCredential::new)
    }

    pub fn start_initial(
        &mut self,
        supervisor: &mut SidecarSupervisor,
    ) -> Result<(), SidecarLaunchError> {
        supervisor
            .validate_load_revalidate_spawn(|| self.prepare_launch())
            .map_err(map_validated_spawn_error)?;
        self.run_policy.has_run_successfully = true;
        self.run_policy.restart_allowed = true;
        Ok(())
    }

    pub fn restart_after_unexpected_exit(
        &mut self,
        supervisor: &mut SidecarSupervisor,
    ) -> Result<(), SidecarLaunchError> {
        if !self.run_policy.has_run_successfully
            || !self.run_policy.restart_allowed
            || self.run_policy.revoked
        {
            return Err(SidecarLaunchError::RestartNotAllowed);
        }
        supervisor
            .validate_load_revalidate_spawn(|| self.prepare_launch())
            .map_err(map_validated_spawn_error)
    }

    pub fn stop(&mut self, supervisor: &mut SidecarSupervisor) -> Result<(), SidecarLaunchError> {
        self.run_policy.restart_allowed = false;
        supervisor
            .stop()
            .map(|_| ())
            .map_err(SidecarLaunchError::Sidecar)
    }

    pub fn revoke_and_stop(
        &mut self,
        supervisor: &mut SidecarSupervisor,
    ) -> Result<(), SidecarLaunchError> {
        self.run_policy.restart_allowed = false;
        if self.run_policy.revoked {
            return Ok(());
        }
        self.run_policy.revoked = true;
        if supervisor.child_pid().is_some() {
            supervisor.stop().map_err(SidecarLaunchError::Sidecar)?;
        }
        self.credential_provider
            .revoke()
            .map_err(SidecarLaunchError::Credential)
    }
}
