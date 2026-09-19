'use strict';

// 测试用最小 PE 构造器（2026-09-19，claim windows-popup-free 的信任门一侧）。
//
// 为什么必须有它：`scripts/lib/sidecar-trust.js` 的 `isPeBinary` 初版只读前 2 个字节判
// "MZ"，于是测试里 `Buffer.concat([Buffer.from([0x4d, 0x5a]), Buffer.alloc(n)])` 这种
// **只有魔数**的桩能一路通过 —— "PE 来源"的断言实际什么都没验。收紧 isPeBinary 后，
// 桩本身也必须是真 PE 结构，否则收紧方向会被这些桩变成假红。
//
// 产出结构合法的最小映像：
//     MZ → e_lfanew(0x3C) → "PE\0\0" → COFF 头(20) → OptionalHeader
//     OptionalHeader.Magic（0x10b PE32 / 0x20b PE32+）与 .Subsystem 放在正确偏移。
//
// 刻意只构造到 OptionalHeader 的 magic/subsystem 偏移为止，不做成"完整可加载映像"：
// 本构造器只服务于"这是不是 PE"这一事实判据，多构造无助于区分真假。

const PE_SIGNATURE = 0x00004550; // "PE\0\0" 的小端读法
const OPTIONAL_MAGIC_PE32 = 0x10b;
const OPTIONAL_MAGIC_PE32_PLUS = 0x20b;
const DEFAULT_E_LFANEW = 0x80;

// 偏移与 scripts/pe-subsystem-verify.py 同口径（pe+24=magic，pe+24+68=subsystem），
// 便于两侧互相印证。
const OPTIONAL_MAGIC_OFFSET = 24;
const OPTIONAL_SUBSYSTEM_OFFSET = 24 + 68;

const GUI_SUBSYSTEM = 2;
const CUI_SUBSYSTEM = 3;

function peBytes(options = {}) {
  const {
    magic = OPTIONAL_MAGIC_PE32_PLUS,
    subsystem = GUI_SUBSYSTEM,
    eLfanew = DEFAULT_E_LFANEW,
    size = 0,
    fill = 0x41,
  } = options;
  const minimum = eLfanew + OPTIONAL_SUBSYSTEM_OFFSET + 2;
  const buffer = Buffer.alloc(Math.max(minimum, size), fill);
  buffer.write('MZ', 0, 'ascii');
  buffer.writeUInt32LE(eLfanew, 0x3c);
  buffer.writeUInt32LE(PE_SIGNATURE, eLfanew);
  buffer.writeUInt16LE(magic, eLfanew + OPTIONAL_MAGIC_OFFSET);
  buffer.writeUInt16LE(subsystem, eLfanew + OPTIONAL_SUBSYSTEM_OFFSET);
  return buffer;
}

// "只有 MZ 魔数"的伪 PE：团队 2026-09-19 实测用它拿到过 isPeBinary()===true。
// 保留为显式构造器，避免各测试各写一份而漂移。
function mzOnlyBytes(size, byte = 0x41) {
  const buffer = Buffer.alloc(size, byte);
  buffer.write('MZ', 0, 'ascii');
  return buffer;
}

module.exports = {
  CUI_SUBSYSTEM,
  DEFAULT_E_LFANEW,
  GUI_SUBSYSTEM,
  OPTIONAL_MAGIC_OFFSET,
  OPTIONAL_MAGIC_PE32,
  OPTIONAL_MAGIC_PE32_PLUS,
  OPTIONAL_SUBSYSTEM_OFFSET,
  PE_SIGNATURE,
  mzOnlyBytes,
  peBytes,
};
