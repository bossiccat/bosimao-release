'use strict';

// P1 缺陷契约（2026-09-01）：WorkBuddy 等宿主进程会向子进程同时注入大小写
// 两个变体的代理变量（HTTPS_PROXY 与 https_proxy 同存）。Windows 环境变量
// 本身不区分大小写，但 PS 5.1 的 Start-Process 构建子进程环境字典时按区分
// 大小写的 Dictionary 逐条 Add → 「已添加项。字典中的关键字:HTTPS_PROXY/
// 所添加的关键字:https_proxy」ArgumentException，服务拉起直接失败。
// 契约：lib-common.ps1 提供单一实现的 Merge-DuplicateProxyEnv（实测行为：
// SetEnvironmentVariable(name, null) 每次只删一个变体，必须循环删尽后重设），
// 且 jax-services.ps1 中每个 Start-Process -FilePath 调用点都先经折叠。

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const scriptsDir = path.join(__dirname, '..', '..');
const libCommon = fs.readFileSync(path.join(scriptsDir, 'scripts', 'lib-common.ps1'), 'utf8');
const services = fs.readFileSync(path.join(scriptsDir, 'scripts', 'jax-services.ps1'), 'utf8');

test('lib-common.ps1 defines Merge-DuplicateProxyEnv as the single implementation', () => {
  assert.match(libCommon, /function\s+Merge-DuplicateProxyEnv\b/i);
});

test('fold loops SetEnvironmentVariable-delete until the name reads back null', () => {
  const defStart = libCommon.search(/function\s+Merge-DuplicateProxyEnv\b/i);
  assert.ok(defStart > -1, 'function must exist');
  const nextFn = libCommon.slice(defStart + 10).search(/\nfunction\s/i);
  const body = libCommon.slice(defStart, nextFn > -1 ? defStart + 10 + nextFn : undefined);
  // 实测：一次 SetEnvironmentVariable(null) 只删除一个大小写变体，必须循环删尽，
  // 否则残留变体仍会让 Start-Process 的环境字典冲突。
  assert.match(body, /while\s*\(/, 'must loop the delete');
  assert.match(body, /SetEnvironmentVariable\(\$name,\s*\$null/i, 'delete via SetEnvironmentVariable(name, null)');
  // 空值语义：Windows 上 SetEnvironmentVariable(name, '') 等价删除，空值变体只删不重设。
  assert.match(body, /SetEnvironmentVariable\(\$name,\s*\$value/i, 're-set the canonical value');
});

test('every Start-Process launch site in jax-services.ps1 is preceded by the fold', () => {
  const launchSites = (services.match(/Start-Process\s+-FilePath/g) || []).length;
  assert.ok(launchSites > 0, 'jax-services.ps1 must still contain its launch sites');
  const foldCalls = (services.match(/Merge-DuplicateProxyEnv\s*$/gm) || []).length;
  assert.ok(
    foldCalls >= launchSites,
    `each Start-Process site needs a fold call: fold=${foldCalls} < sites=${launchSites}`,
  );
});

test('the relay launch path calls the fold right before Start-Process', () => {
  // relay 启动即本次事故现场（jax-services.ps1:325 一带）：折叠调用必须
  // 出现在 Start-Process -FilePath $PyW（relay）之前、且位于同一启动函数内。
  const relayIdx = services.indexOf('$p = Start-Process -FilePath $PyW');
  assert.ok(relayIdx > -1, 'relay Start-Process must exist');
  const before = services.slice(Math.max(0, relayIdx - 600), relayIdx);
  assert.match(before, /Merge-DuplicateProxyEnv/i, 'fold must be called immediately before the relay launch');
});
