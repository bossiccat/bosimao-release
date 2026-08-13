//! Tauri sidecar supervisor 集成测试：校验、单实例、固定参数、watchdog、退出与自启。

mod support;

use std::path::PathBuf;
use std::time::Duration;

use jax_pet::credential::SecretString;
use jax_pet::sidecar::{
    toggle_autostart, AutoStart, SidecarError, SidecarSpec, SidecarState, SidecarSupervisor,
    ValidatedSpawnError,
};
use jax_pet::sidecar_credential::LaunchCredential;

/// stub 进程入口：由 supervisor 以 `--exact stub_worker --nocapture` 拉起。
/// 无 STUB_ENV 时（正常测试运行）直接通过，不参与断言。
#[test]
fn stub_worker() {
    let args: Vec<String> = std::env::args().collect();
    let Some(mode) = args.iter().find_map(|arg| arg.strip_prefix("--stub-mode=")) else {
        return;
    };
    if let Some(path) = args
        .iter()
        .find_map(|arg| arg.strip_prefix("--stub-args-file="))
    {
        let _ = std::fs::write(path, args.join("\n"));
    }
    match mode {
        // 优雅退出：stdin 收到 shutdown 行后 exit(0)
        "graceful" => {
            let mut line = String::new();
            let _ = std::io::stdin().read_line(&mut line);
            std::process::exit(0);
        }
        // 立即崩溃：模拟 sidecar 启动即挂
        "crash" => std::process::exit(1),
        // 挂死：不读 stdin，等待被终止
        "hang" => std::thread::sleep(Duration::from_secs(600)),
        _ => std::thread::sleep(Duration::from_secs(600)),
    }
}

fn spec(mode: &str, expected_sha256: String, mut args: Vec<String>) -> SidecarSpec {
    let fixture = support::sidecar_fixture();
    args.extend(["--".into(), format!("--stub-mode={mode}")]);
    SidecarSpec {
        binary_path: fixture.binary_path.clone(),
        expected_sha256: if expected_sha256.is_empty() {
            sha256_of(&fixture.binary_path)
        } else {
            expected_sha256
        },
        integrity: fixture.integrity,
        args,
        // TLS 信任锚路径（ADR-020 A1）：stub 测试不读该文件，用占位路径即可。
        ca_cert_path: PathBuf::from("certs/ca.crt"),
        // stub 为 test harness 二进制，启动初始化较慢；5s 确保完整测试负载下也能完成
        graceful_timeout: Duration::from_secs(5),
        kill_timeout: Duration::from_secs(10),
    }
}

fn start(supervisor: &mut SidecarSupervisor) -> Result<(), SidecarError> {
    supervisor
        .validate_load_revalidate_spawn(|| {
            Ok::<_, ()>(LaunchCredential::new(
                SecretString::parse_utf8(vec![b't'; 32]).expect("valid test secret"),
            ))
        })
        .map_err(|error| match error {
            jax_pet::sidecar::ValidatedSpawnError::Validation(error)
            | jax_pet::sidecar::ValidatedSpawnError::Spawn(error) => error,
            jax_pet::sidecar::ValidatedSpawnError::Load(()) => unreachable!(),
        })
}

fn sha256_of(path: &PathBuf) -> String {
    use sha2::{Digest, Sha256};
    let bytes = std::fs::read(path).expect("read binary");
    let mut hasher = Sha256::new();
    hasher.update(&bytes);
    format!("{:x}", hasher.finalize())
}

fn stub_args_file() -> PathBuf {
    let p = std::env::temp_dir().join(format!("sidecar_stub_args_{}.txt", std::process::id()));
    let _ = std::fs::remove_file(&p);
    p
}

// ---- 1. externalBin 存在性 ----

#[test]
fn start_fails_when_binary_missing() {
    let s = SidecarSpec {
        binary_path: PathBuf::from("Z:/definitely/not/exists/sidecar.exe"),
        ..spec("idle", String::new(), vec![])
    };
    let mut sup = SidecarSupervisor::new(s);
    match start(&mut sup) {
        Err(SidecarError::BinaryMissing(_)) => {}
        other => panic!("expected BinaryMissing, got {other:?}"),
    }
    assert_eq!(sup.state(), SidecarState::Stopped);
}

#[test]
fn start_succeeds_when_binary_exists_and_hash_matches() {
    let mut sup = SidecarSupervisor::new(spec(
        "idle",
        String::new(),
        vec!["--exact".into(), "stub_worker".into(), "--nocapture".into()],
    ));
    start(&mut sup).expect("start with valid hash");
    assert_eq!(sup.state(), SidecarState::Running);
    sup.stop().expect("stop");
    assert_eq!(sup.state(), SidecarState::Stopped);
}

// ---- 2. 哈希失败拒绝启动 ----

#[test]
fn start_rejects_hash_mismatch_without_spawn() {
    let mut sup = SidecarSupervisor::new(spec(
        "idle",
        "0".repeat(64), // 与实际文件哈希必然不匹配
        vec!["--exact".into(), "stub_worker".into(), "--nocapture".into()],
    ));
    match start(&mut sup) {
        Err(SidecarError::HashMismatch { .. }) => {}
        other => panic!("expected HashMismatch, got {other:?}"),
    }
    assert_eq!(sup.state(), SidecarState::Stopped);
    assert!(sup.child_pid().is_none(), "no process must be spawned");
}

// ---- 3. 单实例复用 ----

fn assert_load_tamper_blocked(manifest: bool) {
    let args_file = stub_args_file();
    let launch_spec = spec("idle", String::new(), vec![]);
    let tamper_path = if manifest {
        launch_spec.integrity.manifest_path.clone()
    } else {
        launch_spec.integrity.runtime_dir.join("ffmpeg.dll")
    };
    let mut supervisor = SidecarSupervisor::new(launch_spec);
    let result = supervisor.validate_load_revalidate_spawn(|| {
        std::fs::write(tamper_path, "tampered during credential load").expect("tamper file");
        Ok::<_, ()>(LaunchCredential::new(
            SecretString::parse_utf8(vec![b't'; 32]).expect("valid test secret"),
        ))
    });
    let Err(ValidatedSpawnError::Validation(error)) = result else {
        panic!("expected revalidation failure");
    };
    assert!(if manifest {
        matches!(error, SidecarError::ManifestDigestMismatch { .. })
    } else {
        matches!(error, SidecarError::RuntimeHashMismatch(_))
    });
    assert!(supervisor.state() == SidecarState::Stopped && supervisor.child_pid().is_none());
    assert!(!args_file.exists());
}

#[test]
fn runtime_tamper_during_load_is_revalidated_before_spawn() {
    assert_load_tamper_blocked(false);
}

#[test]
fn manifest_tamper_during_load_is_revalidated_before_spawn() {
    assert_load_tamper_blocked(true);
}

#[test]
fn second_start_while_running_returns_already_running() {
    let mut sup = SidecarSupervisor::new(spec(
        "idle",
        String::new(),
        vec!["--exact".into(), "stub_worker".into(), "--nocapture".into()],
    ));
    start(&mut sup).expect("first start");
    match start(&mut sup) {
        Err(SidecarError::AlreadyRunning) => {}
        other => panic!("expected AlreadyRunning, got {other:?}"),
    }
    sup.stop().expect("stop");
    // 停止后可再次启动：单实例生命周期可复用
    start(&mut sup).expect("restart after stop");
    sup.stop().expect("final stop");
}

// ---- 4. 固定参数 capability ----

#[test]
fn spawned_process_receives_only_fixed_args() {
    let args_file = stub_args_file();
    let mut spec = spec(
        "idle",
        String::new(),
        vec!["--exact".into(), "stub_worker".into(), "--nocapture".into()],
    );
    spec.args.extend([
        format!("--stub-args-file={}", args_file.display()),
        "--role=sidecar".into(),
        "--bridge=ws://127.0.0.1:9301".into(),
    ]);
    let mut sup = SidecarSupervisor::new(spec);
    start(&mut sup).expect("start");
    // 等待 stub 落盘 argv（harness 初始化较慢）
    for _ in 0..200 {
        if args_file.exists() {
            break;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    let recorded = std::fs::read_to_string(&args_file).unwrap_or_default();
    let args: Vec<&str> = recorded.lines().collect();
    assert!(
        args.iter().any(|a| *a == "--role=sidecar"),
        "fixed role arg missing: {args:?}"
    );
    assert!(
        args.iter().any(|a| *a == "--bridge=ws://127.0.0.1:9301"),
        "fixed bridge arg missing: {args:?}"
    );
    // capability：supervisor 不接受调用方注入任意参数（API 无参数入口），
    // 并禁止任何 shell/任意可执行参数组合
    assert!(sup.allowed_args().iter().all(|a| {
        !a.starts_with("--eval") && !a.contains("powershell") && !a.contains("cmd.exe")
    }));
    sup.stop().expect("stop");
}

// ---- 5. 退出清理：先优雅后终止 ----

#[test]
fn stop_uses_graceful_shutdown_when_child_cooperates() {
    let mut sup = SidecarSupervisor::new(spec(
        "graceful",
        String::new(),
        vec!["--exact".into(), "stub_worker".into(), "--nocapture".into()],
    ));
    start(&mut sup).expect("start");
    let code = sup.stop().expect("graceful stop");
    assert_eq!(code, 0, "graceful stub must exit 0");
    assert_eq!(sup.state(), SidecarState::Stopped);
}

#[test]
fn stop_kills_hung_child_after_graceful_timeout() {
    let mut sup = SidecarSupervisor::new(spec(
        "hang",
        String::new(),
        vec!["--exact".into(), "stub_worker".into(), "--nocapture".into()],
    ));
    start(&mut sup).expect("start");
    // 优雅写入会因 stub 不读 stdin 而超时，随后强制终止
    let code = sup.stop().expect("stop must terminate hung child");
    assert_ne!(code, 0, "hung stub killed by supervisor, non-zero exit");
    assert_eq!(sup.state(), SidecarState::Stopped);
}

// ---- 7. 托盘自启开关 ----

struct MemoryAutoStart {
    enabled: bool,
    toggles: u32,
}

impl AutoStart for MemoryAutoStart {
    fn is_enabled(&self) -> bool {
        self.enabled
    }
    fn set_enabled(&mut self, enabled: bool) -> Result<(), String> {
        self.enabled = enabled;
        self.toggles += 1;
        Ok(())
    }
}

#[test]
fn autostart_toggle_flips_state_and_is_idempotent() {
    let mut store = MemoryAutoStart {
        enabled: false,
        toggles: 0,
    };
    toggle_autostart(&mut store).expect("enable");
    assert!(store.is_enabled());
    // 幂等：已开启时再 toggle 关闭
    toggle_autostart(&mut store).expect("disable");
    assert!(!store.is_enabled());
    assert_eq!(store.toggles, 2);
}
