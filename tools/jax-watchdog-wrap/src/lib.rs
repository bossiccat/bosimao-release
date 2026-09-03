//! Pure, unit-testable logic for jax-watchdog-wrap.
//!
//! Contract:
//! - `resolve_script_path`: walk ancestors from the exe dir looking for
//!   `scripts/jax-watchdog.ps1` so the wrapper works wherever it is placed
//!   inside the repo tree (e.g. `<repo>/bin`, `<repo>/tools/.../release`).
//! - `build_powershell_args`: deterministic powershell invocation —
//!   -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "<script>"
//!   (quoting is mandatory: repo path contains spaces / CJK).

use std::path::{Path, PathBuf};

pub const WATCHDOG_SCRIPT_REL: &str = "scripts/jax-watchdog.ps1";

pub fn resolve_script_path(start: &Path) -> Option<PathBuf> {
    let mut dir = Some(start.to_path_buf());
    while let Some(d) = dir {
        let candidate = d.join("scripts").join("jax-watchdog.ps1");
        if candidate.is_file() {
            return Some(candidate);
        }
        dir = d.parent().map(|p| p.to_path_buf());
    }
    None
}

pub fn build_powershell_args(script: &Path) -> Vec<String> {
    vec![
        "-NoProfile".to_string(),
        "-NonInteractive".to_string(),
        "-ExecutionPolicy".to_string(),
        "Bypass".to_string(),
        "-File".to_string(),
        script.display().to_string(),
    ]
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::path::PathBuf;

    fn temp_root(tag: &str) -> PathBuf {
        let base = std::env::temp_dir().join(format!("jww-test-{}-{}", tag, std::process::id()));
        let _ = fs::remove_dir_all(&base);
        fs::create_dir_all(&base).unwrap();
        base
    }

    #[test]
    fn resolve_finds_script_in_ancestor() {
        let root = temp_root("find");
        let deep = root.join("tools").join("jax-watchdog-wrap").join("target").join("release");
        fs::create_dir_all(&deep).unwrap();
        fs::create_dir_all(root.join("scripts")).unwrap();
        fs::write(root.join("scripts").join("jax-watchdog.ps1"), "# stub").unwrap();

        let found = resolve_script_path(&deep).expect("should find script upward");
        assert_eq!(
            found,
            root.join("scripts").join("jax-watchdog.ps1"),
            "must return the exact ancestor script path"
        );
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn resolve_returns_none_when_absent() {
        let root = temp_root("absent");
        let deep = root.join("a").join("b");
        fs::create_dir_all(&deep).unwrap();
        assert!(
            resolve_script_path(&deep).is_none(),
            "no scripts/jax-watchdog.ps1 anywhere up the tree -> None"
        );
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn resolve_prefers_nearest_ancestor() {
        let root = temp_root("nearest");
        let inner = root.join("inner");
        fs::create_dir_all(inner.join("scripts")).unwrap();
        fs::create_dir_all(root.join("scripts")).unwrap();
        fs::write(inner.join("scripts").join("jax-watchdog.ps1"), "# inner").unwrap();
        fs::write(root.join("scripts").join("jax-watchdog.ps1"), "# outer").unwrap();

        let leaf = inner.join("bin");
        fs::create_dir_all(&leaf).unwrap();
        let found = resolve_script_path(&leaf).expect("should find nearest");
        assert!(found.starts_with(&inner), "nearest ancestor wins: {}", found.display());
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn args_contain_hardening_flags_in_order() {
        let script = Path::new("C:\\some path\\scripts\\jax-watchdog.ps1");
        let args = build_powershell_args(script);
        assert_eq!(args[0], "-NoProfile", "no profile loading");
        assert_eq!(args[1], "-NonInteractive", "no interactive prompt hang");
        assert_eq!(args[2], "-ExecutionPolicy");
        assert_eq!(args[3], "Bypass");
        assert_eq!(args[4], "-File");
        // RAW path, no manual quotes: std::process::Command applies Windows
        // argument quoting itself; pre-quoting produced ""..."" and
        // powershell -File rejected it ("路径中具有非法字符", exit 127).
        assert_eq!(args[5], "C:\\some path\\scripts\\jax-watchdog.ps1");
    }

    #[test]
    fn args_pass_cjk_path_unquoted() {
        let script = Path::new("C:\\Users\\Administrator\\WorkBuddy\\监视app\\scripts\\jax-watchdog.ps1");
        let args = build_powershell_args(script);
        assert!(
            !args[5].starts_with('"') && !args[5].ends_with('"'),
            "must NOT pre-quote: {}",
            args[5]
        );
        assert_eq!(args[5], "C:\\Users\\Administrator\\WorkBuddy\\监视app\\scripts\\jax-watchdog.ps1");
    }
}
