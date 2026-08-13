'use strict';

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
  } catch (_) {
    logFatal();
    requestFatal();
    return false;
  }
  scheduleInterval(pollAndJoin, 2000);
  scheduleTimeout(pollAndJoin, 300);
  return true;
}

module.exports = { startPollingRuntime };
