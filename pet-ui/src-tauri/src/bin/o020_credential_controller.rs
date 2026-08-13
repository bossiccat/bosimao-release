#![cfg(all(windows, feature = "credential-test-support"))]

use std::io::Read;
use std::process::ExitCode;

use jax_pet::o020_controller;
use zeroize::Zeroizing;

const MAX_STDIN: usize = 512;

fn main() -> ExitCode {
    let mut bytes = Zeroizing::new(Vec::new());
    let read = std::io::stdin()
        .take((MAX_STDIN + 1) as u64)
        .read_to_end(&mut bytes);
    if read.is_err() || bytes.len() > MAX_STDIN {
        return ExitCode::from(12);
    }
    match o020_controller::run_matrix(bytes) {
        Ok(manifest) => match serde_json::to_string(&manifest) {
            Ok(json) => {
                println!("{json}");
                ExitCode::SUCCESS
            }
            Err(_) => ExitCode::from(40),
        },
        Err(_) => ExitCode::from(40),
    }
}
