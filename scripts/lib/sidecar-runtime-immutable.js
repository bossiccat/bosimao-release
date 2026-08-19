'use strict';

// ADR-027 §6：final generation 的不可变保护与 publisher 回收前的可写恢复。
// 只读标记阻止 reader 身份在运行时写入 generation；读取/校验不受影响，
// pointer replacement 只写 generation 外的 current.json 也不受影响。
// Windows 上 chmod 的写位映射为 read-only attribute（写入触发 EPERM）；
// 目录级 NTFS ACL 强制属 Task 9 原生范围。

const fs = require('node:fs');
const path = require('node:path');

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
  makeImmutableGeneration,
  restoreWritableGeneration,
};
