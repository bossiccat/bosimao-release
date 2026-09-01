'use strict';

// P1 回归契约（2026-09-01，976c98f 引入）：Get-RelayProcesses 的命令行正则
// 写在 PS 单引号字符串里却用了 Bash/JS 风格的 \\s 双重转义。PS 单引号字符串
// 不处理反斜杠 → regex 引擎收到 [\\s"'] = {反斜杠, s, ", '}，不含空格 →
// "-m backend.relay.relay_client"（空格分隔）永远匹配不上 → Get-RelayProcesses
// 恒空 → relay 健康判定恒假：watchdog 每 5 分钟把健康的 relay 判为异常并反复
// spawn 新实例（叠加代理折叠修复后即进程泄漏）。
// 契约：正则必须用真实空白类 \s（单引号字符串中单反斜杠即字面传给 regex）。

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const libCommon = fs.readFileSync(
  path.join(__dirname, '..', '..', 'scripts', 'lib-common.ps1'),
  'utf8',
);

function extractCommandlinePattern() {
  const m = libCommon.match(/CommandLine\s+-match\s+'([^']*)'/);
  return m ? m[1] : null;
}

test('Get-RelayProcesses has a commandline match pattern', () => {
  assert.ok(extractCommandlinePattern(), 'CommandLine -match must exist in lib-common.ps1');
});

test('the pattern uses a real whitespace class, not double-escaped backslash-s', () => {
  const pattern = extractCommandlinePattern();
  assert.ok(pattern);
  // node 正则 /\\\\s/ = 字面两反斜杠+s；PS 单引号串里的 \\s 会以 "\\s" 传给
  // regex 引擎（转义反斜杠 + 字面 s），空白类完全失效 —— 必须杜绝。
  assert.doesNotMatch(pattern, /\\\\s/, 'double-escaped \\s never matches whitespace in PS single-quoted strings');
  assert.match(pattern, /\\s/, 'must use a real whitespace class');
});

test('the pattern still anchors the full relay module token', () => {
  const pattern = extractCommandlinePattern();
  assert.ok(pattern);
  assert.match(pattern, /-m/);
  assert.match(pattern, /backend\\.relay\\.relay_client/);
});
