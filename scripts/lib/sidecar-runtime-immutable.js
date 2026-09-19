'use strict';

// ADR-027 §6：final generation 的不可变保护与 publisher 回收前的可写恢复。
// 只读标记阻止 reader 身份在运行时写入 generation；读取/校验不受影响，
// pointer replacement 只写 generation 外的 current.json 也不受影响。
// Windows 上 chmod 的写位映射为 read-only attribute（写入触发 EPERM）；
// 目录级 NTFS ACL 强制属 Task 9 原生范围。

const fs = require('node:fs');
const path = require('node:path');

// 一个 generation 目录里，这些**顶层**名字是运行期可再生产物，不属于载荷闭集：
// Chromium 在 CWD（= generation 根）写 debug.log 且**每次启动都在追加**，内容由运行期决定。
// Rust 自 RP-07（2026-09-02，v4k/v4m 实测）起已在 list_runtime_files 与 walk_generation_payload
// 的 walk/expected 两侧豁免；构建侧此前一处都没有跟上，于是"构建期声明的哈希"与"运行期实际
// 内容"必然分叉（本机现役世代实测：清单 70aa1d9b… / 实测 78cf15e5…）。这里是 JS 侧唯一
// 真相源，与 Rust 侧同集合由 sidecar-package.test.js 的跨语言锁钉住。
// 边界：只列**已证实**的产物，不做目录级/通配豁免；不是穷举，扩列必须有实测支撑。
const RUNTIME_ARTIFACT_FILES = ['debug.log'];

function makeImmutableGeneration(generationDir) {
  for (const entry of fs.readdirSync(generationDir, { withFileTypes: true })) {
    const target = path.join(generationDir, entry.name);
    const stat = fs.lstatSync(target);
    if (stat.isSymbolicLink()) continue; // 上游已拒绝，防御性跳过。
    if (entry.isDirectory()) {
      makeImmutableGeneration(target);
      fs.chmodSync(target, 0o555);
    } else if (entry.isFile()) {
      fs.chmodSync(target, 0o444);
    }
  }
  fs.chmodSync(generationDir, 0o555);
}

// GC（publisher 身份）删除前恢复可写位，使只读 generation 可被回收。
// 失败不影响 GC 语义：保留并在下次重试。
function restoreWritableGeneration(generationDir) {
  if (!fs.existsSync(generationDir)) return;
  for (const entry of fs.readdirSync(generationDir, { withFileTypes: true })) {
    const target = path.join(generationDir, entry.name);
    const stat = fs.lstatSync(target);
    if (stat.isSymbolicLink()) continue;
    if (entry.isDirectory()) {
      restoreWritableGeneration(target);
      fs.chmodSync(target, 0o755);
    } else if (entry.isFile()) {
      fs.chmodSync(target, 0o644);
    }
  }
  fs.chmodSync(generationDir, 0o755);
}

module.exports = {
  RUNTIME_ARTIFACT_FILES,
  makeImmutableGeneration,
  restoreWritableGeneration,
};
