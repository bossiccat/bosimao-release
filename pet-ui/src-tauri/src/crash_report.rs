//! crash_report.rs — 全局 panic hook 落盘（E-1 崩溃上报）。
//!
//! 边界：只在 panic 发生时把崩溃信息（消息 + 位置 + 栈回溯 + 时间戳 + 版本 + PID）
//! 落盘到日志目录。**绝不回显敏感信息**——栈回溯与消息只写文件，不上传、不弹窗、
//! 不进 UI；写盘失败只打印 stderr，绝不因日志失败二次 panic（会触发 abort）。

use std::backtrace::Backtrace;
use std::io::Write;
use std::panic::PanicHookInfo;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;
use std::time::{SystemTime, UNIX_EPOCH};

/// 崩溃日志目录。解析优先级：
/// 1. `JAX_CRASH_LOG_DIR` 环境变量（可配置，便于测试/现场定位）；
/// 2. `set_log_dir` 注入的真实 app 日志目录（`app_log_dir()`）；
/// 3. 兜底：当前工作目录 `logs/crash/`（仅覆盖 setup 之前就 panic 的极端情况）。
static LOG_DIR: OnceLock<PathBuf> = OnceLock::new();

/// 在 `main()` 入口尽早注册全局 panic hook。
///
/// 保留默认 hook（继续把 panic 打到 stderr），先落盘再调用默认 hook，
/// 既不吞崩溃信息，也保证崩溃现场优先持久化。
pub fn install_panic_hook() {
    let default_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info: &PanicHookInfo<'_>| {
        if let Err(error) = write_crash_report(info) {
            // 绝不在 hook 内 panic：日志失败只能 stderr 兜底，不能二次崩溃。
            eprintln!("crash_report: failed to persist crash report: {error}");
        }
        default_hook(info);
    }));
}

/// 由 `setup` 注入真实 app 日志目录（`app.path().app_log_dir()`）。
/// 幂等：只设置一次，后续调用为 no-op。
pub fn set_log_dir(dir: PathBuf) {
    let _ = LOG_DIR.set(dir);
}

fn write_crash_report(info: &PanicHookInfo<'_>) -> std::io::Result<()> {
    let dir = LOG_DIR.get().cloned().unwrap_or_else(fallback_log_dir);
    std::fs::create_dir_all(&dir)?;
    let (mut file, path) = open_unique(&dir, &timestamp_filename())?;
    // 崩溃报告只落盘，绝不走 stdout/UI/网络。
    file.write_all(build_report(info).as_bytes())?;
    eprintln!("crash_report: crash persisted to {}", path.display());
    Ok(())
}

/// 兜底目录：优先环境变量，否则当前工作目录下 logs/crash（相对路径，非硬编码绝对路径）。
fn fallback_log_dir() -> PathBuf {
    if let Some(dir) = std::env::var_os("JAX_CRASH_LOG_DIR") {
        return PathBuf::from(dir);
    }
    std::env::current_dir()
        .unwrap_or_else(|_| PathBuf::from("."))
        .join("logs")
        .join("crash")
}

fn build_report(info: &PanicHookInfo<'_>) -> String {
    let mut out = String::new();
    out.push_str("==== JAX-PET CRASH REPORT ====\n");
    out.push_str(&format!("version: {}\n", env!("CARGO_PKG_VERSION")));
    out.push_str(&format!("pid: {}\n", std::process::id()));
    out.push_str(&format!("time: {}\n", timestamp_readable()));
    out.push_str(&format!("thread: {:?}\n", std::thread::current().name()));
    if let Some(loc) = info.location() {
        out.push_str(&format!(
            "location: {}:{}:{}\n",
            loc.file(),
            loc.line(),
            loc.column()
        ));
    }
    out.push_str(&format!("message: {}\n", panic_message(info)));
    // 栈回溯仅落盘，绝不回显到任何对外通道。
    let backtrace = Backtrace::force_capture();
    out.push_str("backtrace:\n");
    out.push_str(&format!("{backtrace}\n"));
    out
}

/// 提取 panic payload 文本：`&str`/`String` 优先，其余用 Debug 兜底。
fn panic_message(info: &PanicHookInfo<'_>) -> String {
    if let Some(s) = info.payload().downcast_ref::<&str>() {
        return (*s).to_string();
    }
    if let Some(s) = info.payload().downcast_ref::<String>() {
        return s.clone();
    }
    format!("{:?}", info.payload())
}

/// 以 `create_new` 打开一个绝不覆盖历史文件的目标路径。
/// 同一时间戳冲突时追加 `-1`、`-2`…，超限退回 PID 后缀兜底。
fn open_unique(dir: &Path, ts: &str) -> std::io::Result<(std::fs::File, PathBuf)> {
    use std::fs::OpenOptions;
    let mut idx = 0u32;
    loop {
        let name = if idx == 0 {
            format!("crash-{ts}.log")
        } else {
            format!("crash-{ts}-{idx}.log")
        };
        let path = dir.join(&name);
        match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(file) => return Ok((file, path)),
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                idx += 1;
                if idx > 999 {
                    let path = dir.join(format!("crash-{ts}-pid{}.log", std::process::id()));
                    return OpenOptions::new()
                        .write(true)
                        .create_new(true)
                        .open(&path)
                        .map(|f| (f, path));
                }
            }
            Err(e) => return Err(e),
        }
    }
}

fn timestamp_filename() -> String {
    let t = now_utc();
    format!(
        "{:04}{:02}{:02}-{:02}{:02}{:02}-{:03}",
        t.year, t.month, t.day, t.hour, t.minute, t.second, t.millis
    )
}

fn timestamp_readable() -> String {
    let t = now_utc();
    format!(
        "{:04}-{:02}-{:02} {:02}:{:02}:{:02}.{:03} UTC",
        t.year, t.month, t.day, t.hour, t.minute, t.second, t.millis
    )
}

#[derive(Debug, Clone, Copy)]
struct CivilTime {
    year: i64,
    month: u32,
    day: u32,
    hour: u32,
    minute: u32,
    second: u32,
    millis: u32,
}

/// 当前 UTC 时间拆解为公历字段（无 chrono 依赖，纯标准库）。
fn now_utc() -> CivilTime {
    let dur = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    let secs = dur.as_secs() as i64;
    let millis = dur.subsec_millis();
    let days = secs.div_euclid(86_400);
    let secs_of_day = secs.rem_euclid(86_400) as u32;
    let (year, month, day) = civil_from_days(days);
    CivilTime {
        year,
        month,
        day,
        hour: secs_of_day / 3600,
        minute: (secs_of_day % 3600) / 60,
        second: secs_of_day % 60,
        millis,
    }
}

/// Howard Hinnant `civil_from_days`：自 1970-01-01 的天数 → 公历 (年, 月, 日)。
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = (if z >= 0 { z } else { z - 146_096 }) / 146_097;
    let doe = z - era * 146_097; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365; // [0, 399]
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let day = (doy - (153 * mp + 2) / 5 + 1) as u32; // [1, 31]
    let month = (if mp < 10 { mp + 3 } else { mp - 9 }) as u32; // [1, 12]
    let year = if month <= 2 { y + 1 } else { y };
    (year, month, day)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn civil_from_days_known_epochs() {
        // 1970-01-01 为第 0 天。
        assert_eq!(civil_from_days(0), (1970, 1, 1));
        // 2000-01-01 = 10957 天（30 年含 7 个闰日）。
        assert_eq!(civil_from_days(10_957), (2000, 1, 1));
        // 2024-02-29 = 19782 天（闰日）。
        assert_eq!(civil_from_days(19_782), (2024, 2, 29));
        // 2024-02-29 次日应为 2024-03-01。
        assert_eq!(civil_from_days(19_783), (2024, 3, 1));
    }
}
