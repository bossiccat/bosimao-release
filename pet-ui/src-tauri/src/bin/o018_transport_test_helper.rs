#![cfg(windows)]

use std::io::Read;
use std::process::ExitCode;
use std::time::Duration;

use zeroize::Zeroizing;

fn main() -> ExitCode {
    let mut input = Zeroizing::new(Vec::new());
    if std::io::stdin().read_to_end(&mut input).is_err() {
        return ExitCode::from(9);
    }
    if input.starts_with(b"timeout-") {
        std::thread::sleep(Duration::from_secs(30));
        return ExitCode::SUCCESS;
    }
    if input.starts_with(b"failure-") {
        return ExitCode::from(7);
    }
    ExitCode::SUCCESS
}
