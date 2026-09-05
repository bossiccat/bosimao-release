'use strict';

// intent-recovery.js —— 兑付失败自愈 + 控制面 fetch 超时（纯逻辑，无 IO 依赖）。
//
// 背景（2026-09-05 真机证据）：
//   backend 事件循环阻塞 111s → sign HTTP 往返挂死占住 sidecar 轮询（pollingBusy）；
//   hello proof TTL=60s 在等待中过期 → 兑付 40112 → bridge 下发
//   ctrl exit reason=hello_redemption_failed → sidecar 进程退出
//   → 用户「停止监听后再点立即监听没反应」。
// 修复语义：
//   - 兑付失败是可恢复事件：退房 + 清会话 + 继续轮询；连续失败达上限才退出
//     （防崩溃循环），任一会话存活超过观察窗即重置计数。
//   - 已死意图（签名被拒/兑付失败）进入有界跳过列表，防止轮询反复选中重签死循环。
//   - 控制面 fetch 一律带 AbortController 超时，网络挂死快速失败、释放轮询。

const DEFAULT_MAX_CONSECUTIVE_FAILURES = 5;
const DEFAULT_FETCH_TIMEOUT_MS = 8000;
const DEFAULT_SKIP_LIST_CAPACITY = 50;

function createRecoveryState({ maxConsecutiveFailures } = {}) {
  const limit = Number.isInteger(maxConsecutiveFailures) && maxConsecutiveFailures > 0
    ? maxConsecutiveFailures
    : DEFAULT_MAX_CONSECUTIVE_FAILURES;
  let consecutive = 0;
  return {
    get consecutiveFailures() { return consecutive; },
    // 返回 true = 继续自愈；false = 连续失败达上限，应退出进程
    recordFailure() {
      consecutive += 1;
      return consecutive < limit;
    },
    reset() { consecutive = 0; },
  };
}

function createIntentSkipList({ capacity = DEFAULT_SKIP_LIST_CAPACITY } = {}) {
  const failed = new Set();
  return {
    has: (sessionId) => failed.has(sessionId),
    add(sessionId) {
      if (!sessionId) return;
      failed.add(sessionId);
      // 有界：Set 迭代序 = 插入序，超出容量丢弃最早记录
      while (failed.size > capacity) {
        failed.delete(failed.values().next().value);
      }
    },
    size: () => failed.size,
  };
}

function fetchJsonWithTimeout(url, options = {}, {
  timeoutMs = DEFAULT_FETCH_TIMEOUT_MS,
  scheduleTimeout = setTimeout,
  cancelTimeout = clearTimeout,
  fetchImpl = fetch,
} = {}) {
  const controller = new AbortController();
  const merged = { ...options, signal: controller.signal };
  // 先发起请求再挂超时定时器：保证 abort 监听在定时器触发前已挂接
  const pending = Promise.resolve(fetchImpl(url, merged))
    .then((resp) => resp.json());
  const timer = scheduleTimeout(() => controller.abort(), timeoutMs);
  return pending.finally(() => cancelTimeout(timer));
}

module.exports = {
  DEFAULT_FETCH_TIMEOUT_MS,
  DEFAULT_MAX_CONSECUTIVE_FAILURES,
  DEFAULT_SKIP_LIST_CAPACITY,
  createIntentSkipList,
  createRecoveryState,
  fetchJsonWithTimeout,
};
