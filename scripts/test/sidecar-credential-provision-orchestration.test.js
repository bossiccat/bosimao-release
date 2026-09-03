'use strict';

// O-018 切片 3 守卫测试（源码完整性，仿 jax-watchdog-sidecar-ownership.test.js 模式）
// 断言 jax-services.ps1 / dev.ps1 编排了 sidecar credential 的
// 「launcher → CM → .env 同值同步」链，且具备 fail-closed / 幂等 / 备份语义。

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.resolve(__dirname, '..', '..');
const servicesPath = path.join(root, 'scripts', 'jax-services.ps1');
const devPath = path.join(root, 'scripts', 'dev.ps1');
const libCommonPath = path.join(root, 'scripts', 'lib-common.ps1');

function sourceOf(p) {
  return fs.readFileSync(p, 'utf8');
}

test('jax-services.ps1 orchestrates sidecar credential provision', () => {
  const source = sourceOf(servicesPath);

  // 编排函数存在
  assert.match(source, /function\s+Invoke-SidecarCredentialProvision\b/i);

  // Start-BackendService 在 owner provision 之后调用（同款 fail-closed 门）
  assert.match(
    source,
    /Invoke-OwnerCredentialProvision[\s\S]{0,400}?Invoke-SidecarCredentialProvision/i,
    'sidecar provision 必须紧跟 owner provision 之后（backend 启动前）'
  );
  assert.match(source, /if\s*\(\s*-not\s*\(Invoke-SidecarCredentialProvision\)\s*\)\s*\{\s*return\s+\$false/i);

  // 真实 launcher（不是手工 CredWriteW / .env 直写绕过）
  assert.match(source, /provision_sidecar_credential_launcher\.exe/i);

  // 幂等分支：CM 与 .env 同值时不跑 launcher 不动盘
  assert.match(source, /幂等跳过/);

  // 失败诊断码惯例
  assert.match(source, /SIDECARPROV_/);
});

test('dev.ps1 orchestrates sidecar credential provision with fail-closed gate', () => {
  const source = sourceOf(devPath);

  assert.match(source, /function\s+Invoke-SidecarCredentialProvision\b/i);
  assert.match(
    source,
    /Invoke-OwnerCredentialProvision[\s\S]{0,200}?Invoke-SidecarCredentialProvision/i
  );
  assert.match(source, /if\s*\(\s*-not\s*\(Invoke-SidecarCredentialProvision\)\s*\)\s*\{\s*throw/i);
});

test('lib-common.ps1 provides CM read and atomic dotenv write helpers', () => {
  const source = sourceOf(libCommonPath);

  // CM 读取走 Win32 CredReadW P/Invoke（禁止 cmdkey 等不可回读方案）
  assert.match(source, /function\s+Get-CredentialBlobFromCM\b/i);
  assert.match(source, /CredReadW/);
  assert.match(source, /CredFree/);

  // .env 单键原子替换：临时文件 + File.Replace（PS 5.1 下 Move 目标存在即抛异常）
  assert.match(source, /function\s+Set-DotEnvValue\b/i);
  assert.match(source, /backup-pre-sidecarprov/);
  assert.match(source, /\[IO\.File\]::Replace\(\$tmp,\s*\$Path,\s*\$backup\)/);
});

test('orchestration never logs the secret itself', () => {
  const joined = sourceOf(servicesPath) + sourceOf(devPath) + sourceOf(libCommonPath);

  // 日志只允许 hash 前缀/长度，不允许把值拼进 Write-Host/Write-Warning/Write-Error
  assert.doesNotMatch(joined, /Write-(Host|Warning|Error)[^\r\n]*\$_(sidecar|cred|secret)/i);
});
