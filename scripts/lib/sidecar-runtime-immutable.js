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

// 2026-09-24（Linux CI 实测事故）：把**顶层目录**的查封拆成可选动作。
//
// 原因是一次真实的平台不对称：POSIX rename(2) 在**移动目录**时要求被移动目录自身可写 ——
// rename(2) 的 EACCES 条款（Linux man-pages 6.19，逐字引用）：
//     "EACCES Write permission is denied for the directory containing oldpath or
//      newpath, or, search permission is denied for one of the directories in the
//      path prefix of oldpath or newpath, or oldpath is a directory and does not
//      allow write permission (needed to update the ..  entry)."
// 而 finalizeStagedGeneration 原先的顺序是「先把 staging 冻结成 0o555 → 再 rename」，
// 于是 Linux 上那次 rename 必然 EACCES ⇒ **publisher 根本发布不出世代**
// （ubuntu 上 sidecar-package.test.js 63 个用例里 29 个红，全是同一个 errno）。
// Windows 容忍重命名只读目录，所以本机一直是绿的、这个缺陷长期没暴露。
//
// 因此拆开两件事：**内容**（子目录/文件）仍在 rename 之前冻结；**顶层目录**延后到
// 最终路径再封印（见 sidecar-runtime-protocol.js 的 finalizeStagedGeneration）。
function makeImmutableGeneration(generationDir, { sealTopDirectory = true } = {}) {
  for (const entry of fs.readdirSync(generationDir, { withFileTypes: true })) {
    const target = path.join(generationDir, entry.name);
    const stat = fs.lstatSync(target);
    if (stat.isSymbolicLink()) continue; // 上游已拒绝，防御性跳过。
    if (entry.isDirectory()) {
      // 子目录**始终**封印：rename 不要求被移动目录的后代可写，所以递归照旧。
      makeImmutableGeneration(target);
      fs.chmodSync(target, 0o555);
    } else if (entry.isFile()) {
      fs.chmodSync(target, 0o444);
    }
  }
  if (sealTopDirectory) fs.chmodSync(generationDir, 0o555);
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

// 落位后查封世代**顶层目录**。
//
// 为什么顶层不能在 rename **之前**就封（2026-09-24 Linux CI 实测事故）：
// POSIX rename(2) 在**移动目录**时要求被移动目录自身可写。rename(2) 的 EACCES 条款
// （Linux man-pages 6.19，逐字引用）：
//     "EACCES Write permission is denied for the directory containing oldpath or
//      newpath, or, search permission is denied for one of the directories in the
//      path prefix of oldpath or newpath, or oldpath is a directory and does not
//      allow write permission (needed to update the ..  entry)."
// 原先的顺序是「staging 冻结成 0o555 → rename」，于是 Linux 上那次 rename 必然 EACCES
// ⇒ publisher 根本发布不出世代（ubuntu 上 sidecar-package.test.js 63 个用例里 29 个红，
// 全部同一个 errno）。Windows 容忍重命名只读目录，所以本机长期是绿的、缺陷一直没暴露。
//
// 目录级封印只能延后；**载荷内容**（子目录 0o555 / 文件 0o444）仍在 rename 之前冻结，
// 因此"冻结失败不得把可写载荷暴露到 generations/"这个意图依然成立。
function sealTopDirectory(generationDir) {
  fs.chmodSync(generationDir, 0o555);
}

module.exports = {
  RUNTIME_ARTIFACT_FILES,
  makeImmutableGeneration,
  restoreWritableGeneration,
  sealTopDirectory,
};
