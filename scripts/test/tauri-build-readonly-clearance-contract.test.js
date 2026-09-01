'use strict';

// P0 回归契约（2026-09-01）：ADR-027 immutable generation 落地后，generation 内
// 4026 个文件被 chmod 0o444/0o555（Windows 只读属性）。tauri-build 的 copy_resources
// 在每次构建脚本执行时都会 copy_file 覆盖 target/<profile>/jrt/ 下的资源副本，而
// fs::copy 会把源文件的只读属性传播给目标副本——下次构建覆盖只读目标即报
// 「拒绝访问。 (os error 5)」，tauri-build 打印错误后 exit(1)，全部 cargo build（含
// 发布打包）被阻断。契约：build.rs 必须在 tauri_build::build() 之前摘除 target
// 资源副本的只读位。

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const buildRsPath = path.join(__dirname, '..', '..', 'pet-ui', 'src-tauri', 'build.rs');
const buildRs = fs.readFileSync(buildRsPath, 'utf8');

test('build.rs defines a target-resource readonly clearance step', () => {
  assert.match(buildRs, /fn clear_readonly_target_resources\(\)/);
});

test('clearance runs after verify and before tauri_build::build() in main', () => {
  const verifyCall = buildRs.indexOf('verify_sidecar_for_release(&manifest_dir);');
  const clearCall = buildRs.indexOf('clear_readonly_target_resources();', verifyCall + 1);
  // lastIndexOf：文档注释里也会出现 "tauri_build::build()" 字样，main 里的调用是最后一次出现。
  const tauriCall = buildRs.lastIndexOf('tauri_build::build()');
  assert.ok(verifyCall > -1, 'verify_sidecar_for_release call must exist');
  assert.ok(clearCall > -1, 'clear_readonly_target_resources() must be called in main');
  assert.ok(tauriCall > -1, 'tauri_build::build() must exist');
  assert.ok(
    verifyCall < clearCall && clearCall < tauriCall,
    'clearance must run after verify and strictly before tauri_build::build()',
  );
});

test('clearance recurses into subdirectories of the target resources tree', () => {
  const defStart = buildRs.indexOf('fn clear_readonly_recursive(');
  assert.ok(defStart > -1, 'a recursive clearance helper must exist');
  const nextFn = buildRs.indexOf('fn ', defStart + 10);
  const body = buildRs.slice(defStart, nextFn > -1 ? nextFn : undefined);
  assert.ok(
    body.includes('clear_readonly_recursive('),
    'helper must recurse into subdirectories (jrt/generations/<gen>/... is deep)',
  );
});

test('clearance flips the readonly permission bit, not by deleting files', () => {
  assert.match(buildRs, /set_readonly\(false\)/);
  assert.doesNotMatch(
    buildRs.slice(buildRs.indexOf('fn clear_readonly_target_resources()')),
    /remove_dir_all/,
    'clearance must not wipe the whole jrt copy (incremental cost of 8052 files + 180MB re-copy is the status quo of copy_resources anyway; deletion is the heavier and riskier tool)',
  );
});

test('clearance derives the profile dir from OUT_DIR and targets the jrt resource root', () => {
  assert.match(buildRs, /OUT_DIR/);
  assert.match(buildRs, /join\("jrt"\)/);
});
