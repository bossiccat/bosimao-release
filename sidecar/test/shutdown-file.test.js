'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');

const { watchShutdownFile } = require('../shutdown-file');

function uniqueShutdownPath() {
  return path.join(
    os.tmpdir(),
    `jax-sidecar-test-${process.pid}-${Date.now()}-${Math.random().toString(36).slice(2)}.shutdown`
  );
}

test('env 未设 = 不启用（返回 null，无定时器）', () => {
  assert.equal(watchShutdownFile(undefined, () => {}), null);
  assert.equal(watchShutdownFile('', () => {}), null);
});

test('文件未出现不触发；出现后回调恰好一次且停止轮询（幂等）', () => {
  return new Promise((resolve, reject) => {
    const shutdownPath = uniqueShutdownPath();
    let calls = 0;
    const timer = watchShutdownFile(shutdownPath, () => { calls += 1; }, { intervalMs: 25 });
    assert.ok(timer, 'timer must be armed');
    setTimeout(() => {
      try {
        assert.equal(calls, 0, '文件未出现不得触发');
        fs.writeFileSync(shutdownPath, '');
      } catch (err) { reject(err); }
    }, 100);
    setTimeout(() => {
      try {
        assert.equal(calls, 1, '文件出现必须恰好触发一次');
        // 文件仍在（父进程侧清理前）：不得二次触发（轮询已停止）
        fs.writeFileSync(shutdownPath, 'again');
      } catch (err) { reject(err); }
    }, 300);
    setTimeout(() => {
      clearInterval(timer);
      try {
        assert.equal(calls, 1, '停止轮询后不得再触发');
        resolve();
      } catch (err) { reject(err); }
    }, 450);
  });
});
