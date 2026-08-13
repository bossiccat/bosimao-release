use std::time::{Duration, Instant};

use jax_pet::watchdog::{
    drive_restart_policy, reset_after_manual_start, HealthWindow, Watchdog, WatchdogAction,
    WatchdogConfig,
};

fn watchdog(max_restarts: u32) -> Watchdog {
    Watchdog::new(WatchdogConfig {
        max_restarts,
        base_backoff: Duration::from_secs(1),
        max_backoff: Duration::from_secs(30),
    })
}

#[test]
fn health_window_resets_only_after_continuous_running() {
    let start = Instant::now();
    let mut health = HealthWindow::new(Duration::from_secs(30));
    assert!(!health.observe(true, start));
    assert!(!health.observe(true, start + Duration::from_secs(29)));
    assert!(health.observe(true, start + Duration::from_secs(30)));
    assert!(!health.observe(true, start + Duration::from_secs(31)));
    assert!(!health.observe(false, start + Duration::from_secs(32)));
}

#[test]
fn manual_start_resets_only_after_success() {
    let mut wd = watchdog(1);
    assert_eq!(wd.on_process_exit(1), WatchdogAction::Restart);
    assert_eq!(wd.on_restart_failure(), WatchdogAction::Fuse);
    reset_after_manual_start(&mut wd, false);
    assert!(wd.is_fused());
    reset_after_manual_start(&mut wd, true);
    assert!(!wd.is_fused());
    assert_eq!(wd.on_process_exit(1), WatchdogAction::Restart);
    assert_eq!(wd.take_restart_delay(), Duration::from_secs(1));
}

#[test]
fn restart_wiring_retries_failures_then_fuses() {
    let mut wd = watchdog(3);
    let mut delays = Vec::new();
    let mut attempts = 0;
    let initial_action = wd.on_process_exit(2);
    let action = drive_restart_policy(
        &mut wd,
        initial_action,
        |delay| delays.push(delay),
        || {
            attempts += 1;
            Err::<(), ()>(())
        },
    );
    assert_eq!(action, WatchdogAction::Fuse);
    assert_eq!(attempts, 3);
    assert_eq!(
        delays,
        vec![
            Duration::from_secs(1),
            Duration::from_secs(2),
            Duration::from_secs(4),
        ]
    );
    assert!(wd.is_fused());
}
