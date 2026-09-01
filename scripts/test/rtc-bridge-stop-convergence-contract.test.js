'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const scriptsDir = path.resolve(__dirname, '..');
const servicesPath = path.join(scriptsDir, 'jax-services.ps1');
const commonPath = path.join(scriptsDir, 'lib-common.ps1');

function source(filePath) {
  return fs.readFileSync(filePath, 'utf8');
}

// rtc-bridge 以 .venv 启动器 re-exec 出真正持有 19092/19093 的子进程，进程树有两层。
// 若停止时只杀 PID 文件里的启动器，子会成为孤儿继续持有端口；随后 Start-RtcBridgeService
// 的 /health 幂等判定会采纳该孤儿，restart 静默空转、代码改动永不生效（2026-09-01 实证）。
test('rtc-bridge stop clears the whole tree before the generic pid-file branch', () => {
  const services = source(servicesPath);

  const treeBranch = /if \(\$Name -eq "rtc-bridge"\) \{/;
  const pidBranch = /if \(\$procId -and \(Test-ProcessAlive \$procId\)\) \{/;

  const treeMatch = services.match(treeBranch);
  const pidMatch = services.match(pidBranch);
  assert.ok(treeMatch, '缺少 rtc-bridge 全树停止分支');
  assert.ok(pidMatch, '缺少通用 PID 文件分支');

  assert.ok(
    treeMatch.index < pidMatch.index,
    'rtc-bridge 全树分支必须早于通用 PID 文件分支，否则会退化为只杀启动器、留下持端口孤儿'
  );
});

test('rtc-bridge stop kills every discovered process and fails closed on residue', () => {
  const services = source(servicesPath);

  const block = services.match(/if \(\$Name -eq "rtc-bridge"\) \{[\s\S]*?\n    \}/);
  assert.ok(block, '无法定位 rtc-bridge 停止分支代码块');
  const body = block[0];

  assert.match(body, /@\(Get-RtcBridgeProcesses\)/, '必须按进程发现函数枚举，而非仅用 PID 文件');
  assert.match(body, /foreach \(\$r in \$rs\) \{ Stop-Process -Id \$r\.ProcessId/, '必须逐个终止发现的进程');
  assert.match(body, /\$left = @\(Get-RtcBridgeProcesses\)/, '必须有二次扫描，收敛 re-exec 竞态');
  assert.match(body, /\$still = @\(Get-RtcBridgeProcesses\)/);
  assert.match(body, /if \(\$still\.Count -gt 0\)[\s\S]*?return \$false/, '残留未退出时必须返回失败（fail-closed），不得假装成功');
  assert.match(body, /Clear-PidFile "rtc-bridge"/);
});

test('rtc-bridge has exactly one stop branch (no unreachable duplicate)', () => {
  const services = source(servicesPath);

  const occurrences = services.match(/\$Name -eq "rtc-bridge"/g) || [];
  assert.equal(
    occurrences.length,
    1,
    'rtc-bridge 停止分支必须唯一；出现多处意味着存在被前序分支屏蔽的不可达死代码'
  );
  assert.doesNotMatch(
    services,
    /\} elseif \(\$Name -eq "rtc-bridge"\)/,
    '不得存在 elseif 形式的 rtc-bridge 分支（会被前序 PID 文件分支屏蔽）'
  );
});

test('rtc-bridge process discovery stays scoped to this project venv and module entry', () => {
  const services = source(servicesPath);

  const fn = services.match(/function Get-RtcBridgeProcesses \{[\s\S]*?\n\}/);
  assert.ok(fn, '缺少 Get-RtcBridgeProcesses 定义');
  const body = fn[0];

  assert.match(body, /Name='python\.exe' OR Name='pythonw\.exe'/, '必须限定解释器进程名，避免误伤');
  assert.match(body, /CommandLine -match "rtc_bridge"/, '必须按命令行白名单定位，禁止盲杀');
});
