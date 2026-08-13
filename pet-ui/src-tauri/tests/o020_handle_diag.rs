#![cfg(all(windows, feature = "credential-test-support"))]

// O-020 句柄泄漏诊断入口（test-only）：逐步执行 controller 真实操作，
// 打印每步 handle_count 增量，用于 residual_handles 归因定位。

#[test]
fn handle_delta_diagnostic() {
    jax_pet::o020_handle_diag::run_diagnostic();
}
