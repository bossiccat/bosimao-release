'use strict';

// 把异常转成**可归因的一行**：类型 + 信息 + 首个业务栈帧。
//
// 为什么要有它（2026-09-12 事故沉淀）
// ----------------------------------
// 原实现的 catch 是 `catch (_) { logFatal(); }` —— **异常被整个吞掉**，只留下
// `[FATAL] SIDECAR_INITIALIZATION_FAILED`。于是「runSidecar() 里到底哪一句抛的」
// 在 main-diag.log / 业务日志 / stdout 里**全都查不到**，只能靠猜和反复试。
// 本项目自己主张「崩溃必须自我解释」（supervisor.py 的 output_tail、cloudapi 的
// 启动校验都这么做），唯独这条路径是黑盒。这里补上。
//
// 注意：只取**第一个**栈帧，且只取一行 —— 控制面/日志不该被整段 stack 淹没。
function describeError(err) {
  if (!err) return 'err=<none>';
  const name = err.name || 'Error';
  const message = err.message !== undefined ? String(err.message) : String(err);
  let frame = '';
  if (typeof err.stack === 'string') {
    const line = err.stack.split('\n').map((l) => l.trim()).find((l) => l.startsWith('at '));
    if (line) frame = ` | ${line}`;
  }
  return `err=${name}: ${message}${frame}`;
}

function startPollingRuntime({
  runSidecar,
  pollAndJoin,
  scheduleInterval,
  scheduleTimeout,
  requestFatal,
  logFatal,
}) {
  try {
    runSidecar();
  } catch (err) {
    // 传详细原因而不是空调用：否则失败不可归因（见 describeError 注释）。
    logFatal(describeError(err));
    requestFatal();
    return false;
  }
  scheduleInterval(pollAndJoin, 2000);
  scheduleTimeout(pollAndJoin, 300);
  return true;
}

module.exports = { startPollingRuntime, describeError };
