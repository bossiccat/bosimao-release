//! watchdog.rs — sidecar 进程监督策略（ADR-017）。
//!
//! 有上限退避与熔断：崩溃次数达到 max_restarts 后进入熔断，不再重启；
//! 健康恢复（on_healthy）清零计数并复位退避。

use std::time::{Duration, Instant};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WatchdogAction {
    /// 什么都不做（未运行或健康检查通过）。
    None,
    /// 允许重启（未超限）。
    Restart,
    /// 熔断：达到重启上限，停止自动重启。
    Fuse,
}

#[derive(Debug, Clone)]
pub struct WatchdogConfig {
    /// 崩溃后的最大重启次数，超过即熔断。
    pub max_restarts: u32,
    /// 退避起始间隔。
    pub base_backoff: Duration,
    /// 退避上限。
    pub max_backoff: Duration,
}

pub struct Watchdog {
    config: WatchdogConfig,
    restarts: u32,
    backoff: Duration,
    fused: bool,
}

impl Watchdog {
    pub fn new(config: WatchdogConfig) -> Self {
        Self {
            backoff: config.base_backoff,
            restarts: 0,
            fused: false,
            config,
        }
    }

    /// 进程退出时调用；code=0 是 controlled，不进入异常重启策略。
    pub fn on_process_exit(&mut self, code: i32) -> WatchdogAction {
        if code == 0 {
            return WatchdogAction::None;
        }
        self.record_failure()
    }

    /// 自动重启本身失败时同样计入连续异常，避免静默留在 Stopped。
    pub fn on_restart_failure(&mut self) -> WatchdogAction {
        self.record_failure()
    }

    fn record_failure(&mut self) -> WatchdogAction {
        if self.fused || self.restarts >= self.config.max_restarts {
            self.fused = true;
            return WatchdogAction::Fuse;
        }
        self.restarts += 1;
        WatchdogAction::Restart
    }

    /// 取得当前重启延迟，再推进到下一档；序列为 base、2*base、4*base。
    pub fn take_restart_delay(&mut self) -> Duration {
        let delay = self.backoff;
        let doubled = self.backoff.saturating_mul(2);
        self.backoff = doubled.min(self.config.max_backoff);
        delay
    }

    /// 成功运行一段时间后调用：复位计数与退避。
    pub fn on_healthy(&mut self) {
        self.restarts = 0;
        self.fused = false;
        self.backoff = self.config.base_backoff;
    }

    pub fn is_fused(&self) -> bool {
        self.fused
    }
}

/// 生产健康窗口：仅连续 Running 达到阈值时触发一次健康复位。
pub struct HealthWindow {
    threshold: Duration,
    running_since: Option<Instant>,
    reported: bool,
}

impl HealthWindow {
    pub fn new(threshold: Duration) -> Self {
        Self {
            threshold,
            running_since: None,
            reported: false,
        }
    }

    pub fn observe(&mut self, running: bool, now: Instant) -> bool {
        if !running {
            self.running_since = None;
            self.reported = false;
            return false;
        }
        let since = *self.running_since.get_or_insert(now);
        if !self.reported && now.saturating_duration_since(since) >= self.threshold {
            self.reported = true;
            return true;
        }
        false
    }
}

pub fn reset_after_manual_start(watchdog: &mut Watchdog, started: bool) {
    if started {
        watchdog.on_healthy();
    }
}

pub fn drive_restart_policy<E, S, R>(
    watchdog: &mut Watchdog,
    mut action: WatchdogAction,
    mut sleep: S,
    mut restart: R,
) -> WatchdogAction
where
    S: FnMut(Duration),
    R: FnMut() -> Result<(), E>,
{
    while action == WatchdogAction::Restart {
        sleep(watchdog.take_restart_delay());
        if restart().is_ok() {
            return WatchdogAction::None;
        }
        action = watchdog.on_restart_failure();
    }
    action
}

#[cfg(test)]
mod tests {
    use super::*;

    fn watchdog(max_restarts: u32) -> Watchdog {
        Watchdog::new(WatchdogConfig {
            max_restarts,
            base_backoff: Duration::from_secs(1),
            max_backoff: Duration::from_secs(30),
        })
    }

    #[test]
    fn controlled_exit_is_not_restarted_or_counted() {
        let mut wd = watchdog(3);
        assert_eq!(wd.on_process_exit(0), WatchdogAction::None);
        assert_eq!(wd.on_process_exit(1), WatchdogAction::Restart);
        assert_eq!(wd.take_restart_delay(), Duration::from_secs(1));
    }

    #[test]
    fn restart_failure_is_bounded_with_one_two_four_backoff() {
        let mut wd = watchdog(3);
        assert_eq!(wd.on_process_exit(2), WatchdogAction::Restart);
        assert_eq!(wd.take_restart_delay(), Duration::from_secs(1));
        assert_eq!(wd.on_restart_failure(), WatchdogAction::Restart);
        assert_eq!(wd.take_restart_delay(), Duration::from_secs(2));
        assert_eq!(wd.on_restart_failure(), WatchdogAction::Restart);
        assert_eq!(wd.take_restart_delay(), Duration::from_secs(4));
        assert_eq!(wd.on_restart_failure(), WatchdogAction::Fuse);
    }

    #[test]
    fn health_window_fires_once_after_continuous_running() {
        let start = Instant::now();
        let mut health = HealthWindow::new(Duration::from_secs(30));
        assert!(!health.observe(true, start));
        assert!(!health.observe(true, start + Duration::from_secs(29)));
        assert!(health.observe(true, start + Duration::from_secs(30)));
        assert!(!health.observe(true, start + Duration::from_secs(31)));
        assert!(!health.observe(false, start + Duration::from_secs(32)));
    }

    #[test]
    fn healthy_or_manual_success_clears_fuse_and_backoff() {
        let mut wd = watchdog(1);
        assert_eq!(wd.on_process_exit(1), WatchdogAction::Restart);
        assert_eq!(wd.take_restart_delay(), Duration::from_secs(1));
        assert_eq!(wd.on_restart_failure(), WatchdogAction::Fuse);
        wd.on_healthy();
        assert!(!wd.is_fused());
        assert_eq!(wd.on_process_exit(1), WatchdogAction::Restart);
        assert_eq!(wd.take_restart_delay(), Duration::from_secs(1));
    }
}
