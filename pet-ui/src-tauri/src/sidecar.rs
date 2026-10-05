//! sidecar.rs — Tauri/Rust supervisor（ADR-017）。
//!
//! 唯一职责：externalBin 存在性与 SHA-256 校验、固定参数启动、单实例、
//! 优雅退出（shutdown 文件信号）与超时强制终止。不链接 TRTC、不处理 PCM。

use std::io::PipeWriter;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use crate::credential::SIDECAR_CREDENTIAL_ENV;
use crate::sidecar_credential::LaunchCredential;
use crate::sidecar_integrity::{sha256_file, validate_hash, validate_runtime};
use crate::sidecar_runtime_pointer::GenerationLease;

/// 固定启动契约：调用方在构造时一次性锁定，运行时不允许注入任意参数。
#[derive(Debug, Clone)]
pub struct IntegritySpec {
    pub manifest_path: PathBuf,
    pub expected_manifest_sha256: String,
    pub runtime_dir: PathBuf,
}

/// 固定启动契约：调用方在构造时一次性锁定，运行时不允许注入任意参数。
#[derive(Debug, Clone)]
pub struct SidecarSpec {
    pub binary_path: PathBuf,
    pub expected_sha256: String,
    pub integrity: IntegritySpec,
    /// 固定参数列表（含 stub 模式标记），由构建期/测试夹具锁定。
    pub args: Vec<String>,
    /// 自签 CA 公钥路径（resource_dir/certs/ca.crt），spawn 时经
    /// NODE_EXTRA_CA_CERTS 注入子进程 env（ADR-020 A1）。
    pub ca_cert_path: PathBuf,
    /// 优雅停止等待窗口。
    pub graceful_timeout: Duration,
    /// 强制终止后等待窗口。
    pub kill_timeout: Duration,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SidecarState {
    Stopped,
    Running,
}

#[derive(Debug)]
pub enum SidecarError {
    BinaryMissing(PathBuf),
    ExpectedHashMissing,
    ExpectedHashInvalid(String),
    ManifestMissing(PathBuf),
    ManifestDigestMissing,
    ManifestDigestInvalid(String),
    ManifestDigestMismatch { expected: String, actual: String },
    ManifestInvalid,
    RuntimeSetMismatch,
    RuntimeHashMismatch(PathBuf),
    RuntimeUntrusted,
    HashMismatch { expected: String, actual: String },
    /// 解析失败（ADR-027 resolver fail-closed）：supervisor 永久 Stopped，拒绝任何启动。
    ResolverFailed(String),
    AlreadyRunning,
    SpawnFailed(String),
    NotRunning,
}

/// 单一 owner：同一时间至多持有一个子进程，start/stop 串行驱动。
pub struct SidecarSupervisor {
    /// `None` 表示 resolver 失败（ADR-027 fail-closed），supervisor 永久 Stopped。
    spec: Option<SidecarSpec>,
    resolve_error: Option<String>,
    child: Option<Child>,
    /// 父进程持有的 sidecar stdin 写端（**已闲置**）。
    ///
    /// 2026-10-02 os error 231 修复：`Stdio::piped()` 在 Windows 上经
    /// `NtCreateNamedPipeFile` + `NtOpenFile`（\Device\NamedPipe\ 单向实例，
    /// 子进程端请求 GENERIC_READ）创建；在命名管道打开被安全软件策略改写的
    /// 宿主上（读打开放行、写打开拒绝的策略恰好相反的组合）该序列 100% 返回
    /// ERROR_PIPE_BUSY(231)，spawn 在 CreateProcessW 之前失败 → 首启弹窗。
    /// 改用 `std::io::pipe()`（kernel32 `CreatePipe` 双工实现）+ `Stdio::from`
    /// 句柄包装：子进程拿到读端，父进程持有写端写 shutdown 行，语义不变。
    ///
    /// 2026-10-05 Electron 43 stdin 双回归迁移（e2e 实锤）：E43 主进程下
    /// stdin 'end' 立即假触发（秒退）+ 'data' 永不触发（shutdown 行死信），
    /// sidecar 已不再读 stdin，优雅停机改走 shutdown 文件信号（见
    /// `shutdown_file`）。管道仅为最小改动面保留，闲置无害。
    child_stdin: Option<PipeWriter>,
    /// shutdown 信号文件路径（spawn 时生成、经 JAX_SIDECAR_SHUTDOWN_FILE 注入
    /// 子进程）。优雅停机 = 创建该文件，sidecar 500ms 轮询到即受控退出。
    /// child 退出时清理（防临时目录残留）。
    shutdown_file: Option<PathBuf>,
    state: SidecarState,
    /// 解析成功时持有的 generation 租约，存活到 child 退出。
    lease: Option<GenerationLease>,
}

#[derive(Debug)]
pub enum ValidatedSpawnError<E> {
    Validation(SidecarError),
    Load(E),
    Spawn(SidecarError),
}

impl SidecarSupervisor {
    pub fn new(spec: SidecarSpec) -> Self {
        Self {
            spec: Some(spec),
            resolve_error: None,
            child: None,
            child_stdin: None,
            shutdown_file: None,
            state: SidecarState::Stopped,
            lease: None,
        }
    }

    /// 解析成功且持有 generation 租约：租约存活到 child 退出（stop/try_wait 释放）。
    pub fn new_with_lease(spec: SidecarSpec, lease: GenerationLease) -> Self {
        Self {
            spec: Some(spec),
            resolve_error: None,
            child: None,
            child_stdin: None,
            shutdown_file: None,
            state: SidecarState::Stopped,
            lease: Some(lease),
        }
    }

    /// resolver 失败时构造：永久 Stopped，任何 start 都返回 ResolverFailed，
    /// 绝不使用 fallback sentinel 路径。
    pub fn unresolved(diagnostic: String) -> Self {
        Self {
            spec: None,
            resolve_error: Some(diagnostic),
            child: None,
            child_stdin: None,
            shutdown_file: None,
            state: SidecarState::Stopped,
            lease: None,
        }
    }

    pub fn state(&self) -> SidecarState {
        self.state
    }

    pub fn child_pid(&self) -> Option<u32> {
        self.child.as_ref().and_then(|c| c.id().into())
    }

    /// 固定参数只读视图，用于 capability 断言。
    pub fn allowed_args(&self) -> &[String] {
        self.spec
            .as_ref()
            .map(|spec| spec.args.as_slice())
            .unwrap_or(&[])
    }

    pub fn validate_binary(&self) -> Result<(), SidecarError> {
        self.validate_for_launch()
    }

    fn validate_for_launch(&self) -> Result<(), SidecarError> {
        let spec = self.spec.as_ref().ok_or_else(|| {
            SidecarError::ResolverFailed(self.resolve_error.clone().unwrap_or_default())
        })?;
        if self.state == SidecarState::Running {
            return Err(SidecarError::AlreadyRunning);
        }
        let bin = &spec.binary_path;
        if !Path::new(bin).is_file() {
            return Err(SidecarError::BinaryMissing(bin.clone()));
        }
        validate_hash(&spec.expected_sha256).map_err(|invalid| {
            if invalid.is_empty() {
                SidecarError::ExpectedHashMissing
            } else {
                SidecarError::ExpectedHashInvalid(invalid)
            }
        })?;
        let actual = sha256_file(bin)?;
        if actual != spec.expected_sha256 {
            return Err(SidecarError::HashMismatch {
                expected: spec.expected_sha256.clone(),
                actual,
            });
        }
        validate_runtime(spec)?;
        Ok(())
    }

    pub fn validate_load_revalidate_spawn<E, F>(
        &mut self,
        load: F,
    ) -> Result<(), ValidatedSpawnError<E>>
    where
        F: FnOnce() -> Result<LaunchCredential, E>,
    {
        self.validate_for_launch()
            .map_err(ValidatedSpawnError::Validation)?;
        let launch = load().map_err(ValidatedSpawnError::Load)?;
        self.validate_for_launch()
            .map_err(ValidatedSpawnError::Validation)?;
        self.spawn_with_credential(launch)
            .map_err(ValidatedSpawnError::Spawn)
    }

    fn spawn_with_credential(&mut self, launch: LaunchCredential) -> Result<(), SidecarError> {
        if self.state == SidecarState::Running {
            return Err(SidecarError::AlreadyRunning);
        }
        let Some(spec) = self.spec.as_ref() else {
            return Err(SidecarError::ResolverFailed(
                self.resolve_error.clone().unwrap_or_default(),
            ));
        };
        // 拷贝 spawn 所需字段，避免在可变赋值（self.child/state）期间持有 spec 借用。
        let binary_path = spec.binary_path.clone();
        let args = spec.args.clone();
        let ca_cert_path = spec.ca_cert_path.clone();
        // ADR-027 §3：child working directory 指向 selected generation 目录，
        // 使 Electron 从 generation 内解析 resources/app/... 相对路径。
        let current_dir = spec.integrity.runtime_dir.clone();
        // 2026-08-13 弹窗修复：Windows 下强制 CREATE_NO_WINDOW，杜绝任何子进程弹窗
        // （即使未来 sidecar 换成 console 子系统二进制）。
        #[cfg(windows)]
        let mut cmd = {
            use std::os::windows::process::CommandExt;
            let mut c = Command::new(&binary_path);
            c.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
            c
        };
        #[cfg(not(windows))]
        let mut cmd = Command::new(&binary_path);
        // stdin 管道（os error 231 修复，2026-10-02）：
        // `Stdio::piped()` 的 NT 单向管道序列（子进程端 GENERIC_READ 相对打开）
        // 在命名管道策略被改写的宿主上 100% 返回 ERROR_PIPE_BUSY(231)。
        // `std::io::pipe()` 走 kernel32 `CreatePipe` 双工实现，不受影响。
        // 2026-10-05 E43 stdin 双回归迁移后 sidecar 不再读 stdin，此管道仅为
        // 最小改动面保留（闲置无害）；shutdown 行语义已由下方 shutdown 文件取代。
        let (stdin_read, stdin_write) =
            std::io::pipe().map_err(|e| SidecarError::SpawnFailed(format!("stdin pipe: {e}")))?;
        // shutdown 文件信号（2026-10-05 Electron 43 stdin 双回归迁移）：
        // E43 主进程下 stdin 'end' 立即假触发（秒退）、'data' 永不触发（死信），
        // 优雅停机改走文件——路径唯一（pid + 纳秒时间戳），经
        // JAX_SIDECAR_SHUTDOWN_FILE 注入；sidecar 500ms 轮询到文件出现即受控
        // 退出。跨平台（Windows 桌面 + Linux CloudRun 容器）、无网络栈、幂等。
        let shutdown_file = std::env::temp_dir().join(format!(
            "jax-sidecar-{}-{}.shutdown",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0)
        ));
        // 唯一路径防御性清理：即使撞名也绝不把陈旧文件当成新信号。
        let _ = std::fs::remove_file(&shutdown_file);
        let child = cmd
            .args(&args)
            .current_dir(&current_dir)
            .env(SIDECAR_CREDENTIAL_ENV, launch.expose())
            .env("NODE_EXTRA_CA_CERTS", &ca_cert_path)
            .env("JAX_SIDECAR_SHUTDOWN_FILE", &shutdown_file)
            // RP-07 补充（2026-09-02）：Electron 在 C++ 引导期（早于任何 JS）读取
            // ELECTRON_RUN_AS_NODE 决定是否退化为纯 Node 模式；main.js 的 JS 层净化
            // 来不及救引导期。宿主（如 WorkBuddy shell）注入的 ELECTRON_RUN_AS_NODE=1
            // 会经继承链抵达 sidecar，使其 spawn 成功后秒退 → watchdog 熔断
            //（本机实测复现；stderr 干净是因为 spawn 全部 Ok、熔断由退出计数驱动）。
            // NODE_OPTIONS 同理在引导期生效，一并掐断；sidecar 自身需要的变量全部
            // 由上面 .env 显式注入，不依赖宿主继承。
            .env_remove("ELECTRON_RUN_AS_NODE")
            .env_remove("NODE_OPTIONS")
            .stdin(Stdio::from(stdin_read))
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| SidecarError::SpawnFailed(e.to_string()))?;
        self.child = Some(child);
        self.child_stdin = Some(stdin_write);
        self.shutdown_file = Some(shutdown_file);
        self.state = SidecarState::Running;
        Ok(())
    }

    /// 先创建 shutdown 信号文件优雅停止，窗口内未退出则强制终止。
    /// 返回最终退出码（0 = 优雅，非 0 = 被终止）。
    pub fn stop(&mut self) -> Result<i32, SidecarError> {
        let mut child = self.child.take().ok_or(SidecarError::NotRunning)?;
        // 优雅停止：创建 shutdown 文件，sidecar 轮询到（500ms 周期）即走受控
        // 退出。写端句柄随手释放——E43 stdin 双回归迁移后 sidecar 不再读
        // stdin，管道仅为最小改动面保留。
        self.child_stdin.take();
        let shutdown_file = self.shutdown_file.take();
        if let Some(file) = &shutdown_file {
            // 文件不存在则创建，存在则覆盖——幂等。
            let _ = std::fs::write(file, b"");
        }
        let graceful_timeout = self
            .spec
            .as_ref()
            .map(|spec| spec.graceful_timeout)
            .unwrap_or_default();
        let deadline = Instant::now() + graceful_timeout;
        let code = loop {
            if let Some(status) = child
                .try_wait()
                .map_err(|e| SidecarError::SpawnFailed(e.to_string()))?
            {
                break status.code().unwrap_or(-1);
            }
            if Instant::now() >= deadline {
                let _ = child.kill();
                let status = child
                    .wait()
                    .map_err(|e| SidecarError::SpawnFailed(e.to_string()))?;
                break status.code().unwrap_or(-1);
            }
            std::thread::sleep(Duration::from_millis(10));
        };
        // 无论 child 是否读到信号都清理文件，防临时目录残留。
        if let Some(file) = &shutdown_file {
            let _ = std::fs::remove_file(file);
        }
        self.state = SidecarState::Stopped;
        // child 已退出：显式释放 generation 租约（ADR-027 §5）。
        self.lease.take();
        Ok(code)
    }

    /// 非阻塞查询退出码；进程已退出时返回 Some(code)。
    pub fn try_wait(&mut self) -> Option<i32> {
        let child = self.child.as_mut()?;
        match child.try_wait().ok()? {
            Some(status) => {
                self.state = SidecarState::Stopped;
                // child 已退出：stdin 写端同步释放，避免句柄残留影响下次 start。
                self.child_stdin.take();
                // child 已退出：清理 shutdown 信号文件（若有），防临时目录残留。
                if let Some(file) = self.shutdown_file.take() {
                    let _ = std::fs::remove_file(file);
                }
                // child 已退出：显式释放 generation 租约。
                self.lease.take();
                Some(status.code().unwrap_or(-1))
            }
            None => None,
        }
    }
}

/// 自启开关抽象：测试用内存实现，生产 Windows 用注册表实现。
pub trait AutoStart {
    fn is_enabled(&self) -> bool;
    fn set_enabled(&mut self, enabled: bool) -> Result<(), String>;
}

/// 托盘开关：翻转自启状态，幂等。
pub fn toggle_autostart(store: &mut dyn AutoStart) -> Result<(), String> {
    let next = !store.is_enabled();
    store.set_enabled(next)
}

// 供 tray.rs 生产使用的 Windows 注册表自启实现。
#[cfg(windows)]
pub mod registry_autostart {
    use super::AutoStart;

    const RUN_KEY: &str = r"Software\Microsoft\Windows\CurrentVersion\Run";
    const APP_NAME: &str = "JaxPet";

    pub struct RegistryAutoStart;

    impl AutoStart for RegistryAutoStart {
        fn is_enabled(&self) -> bool {
            winreg::RegKey::predef(winreg::enums::HKEY_CURRENT_USER)
                .open_subkey(RUN_KEY)
                .and_then(|k| k.get_value::<String, _>(APP_NAME))
                .map(|v| !v.is_empty())
                .unwrap_or(false)
        }

        fn set_enabled(&mut self, enabled: bool) -> Result<(), String> {
            let key = winreg::RegKey::predef(winreg::enums::HKEY_CURRENT_USER)
                .open_subkey_with_flags(RUN_KEY, winreg::enums::KEY_READ | winreg::enums::KEY_WRITE)
                .map_err(|e| e.to_string())?;
            if enabled {
                let exe = std::env::current_exe().map_err(|e| e.to_string())?;
                key.set_value(APP_NAME, &format!("\"{}\"", exe.display()))
                    .map_err(|e| e.to_string())?;
            } else {
                key.delete_value(APP_NAME).map_err(|e| e.to_string())?;
            }
            Ok(())
        }
    }
}
