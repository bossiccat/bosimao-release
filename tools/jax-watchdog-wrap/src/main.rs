//! GUI-subsystem wrapper: runs scripts/jax-watchdog.ps1 with ZERO console flash.
//!
//! Why this exists: the scheduled task "Jax-Watchdog-Every5Min" runs
//! powershell.exe on the interactive desktop — every 5 minutes a console
//! window flashes (commercial blocker). This exe is compiled with
//! `windows_subsystem = "windows"` (no console at all) and launches
//! powershell with CREATE_NO_WINDOW so no window is ever created.
//! The watchdog's own exit code is passed through 1:1.

#![windows_subsystem = "windows"]

use std::os::windows::process::CommandExt;
use std::path::PathBuf;
use std::process::{exit, Command};

use jax_watchdog_wrap::{build_powershell_args, resolve_script_path};

const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const EXIT_NO_SCRIPT: i32 = 2;
const EXIT_SPAWN_FAILED: i32 = 3;

fn main() {
    // Script source: argv[1] override, else walk up from the exe dir.
    let script: PathBuf = match std::env::args().nth(1) {
        Some(a) => PathBuf::from(a),
        None => {
            let exe_dir = std::env::current_exe()
                .ok()
                .and_then(|p| p.parent().map(|d| d.to_path_buf()))
                .unwrap_or_default();
            match resolve_script_path(&exe_dir) {
                Some(p) => p,
                None => exit(EXIT_NO_SCRIPT),
            }
        }
    };

    if !script.is_file() {
        exit(EXIT_NO_SCRIPT);
    }

    let mut cmd = Command::new("powershell.exe");
    cmd.args(build_powershell_args(&script))
        .creation_flags(CREATE_NO_WINDOW);

    match cmd.status() {
        Ok(status) => exit(status.code().unwrap_or(1)),
        Err(_) => exit(EXIT_SPAWN_FAILED),
    }
}
