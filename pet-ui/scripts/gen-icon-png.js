// 生成 512x512 PNG 源图（深色底 + 品牌色核心圆）— 供 `tauri icon` 生成图标集
// 运行：node scripts/gen-icon-png.js
import zlib from "node:zlib";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SIZE = 512;
const BG = [11, 14, 20, 255]; // --bg #0b0e14
const CORE = [56, 189, 248, 255]; // --accent #38bdf8
const CORE_INNER = [37, 99, 235, 255]; // --pet-core-start #2563eb

// --- CRC32 ---
const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();
function crc32(buf) {
  let c = 0xffffffff;
  for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length, 0);
  const typeBuf = Buffer.from(type, "ascii");
  const crcBuf = Buffer.alloc(4);
  crcBuf.writeUInt32BE(crc32(Buffer.concat([typeBuf, data])), 0);
  return Buffer.concat([len, typeBuf, data, crcBuf]);
}

// 逐像素绘制
const raw = Buffer.alloc(SIZE * (SIZE * 4 + 1));
const cx = SIZE / 2;
const cy = SIZE / 2;
const R = SIZE * 0.42;
const rInner = SIZE * 0.16;
for (let y = 0; y < SIZE; y++) {
  const rowStart = y * (SIZE * 4 + 1);
  raw[rowStart] = 0; // filter: None
  for (let x = 0; x < SIZE; x++) {
    const d = Math.hypot(x - cx, y - cy);
    let px;
    if (d <= rInner) px = CORE_INNER;
    else if (d <= R) {
      // 柔和过渡
      const t = (d - rInner) / (R - rInner);
      px = [
        Math.round(CORE_INNER[0] + (CORE[0] - CORE_INNER[0]) * t),
        Math.round(CORE_INNER[1] + (CORE[1] - CORE_INNER[1]) * t),
        Math.round(CORE_INNER[2] + (CORE[2] - CORE_INNER[2]) * t),
        255,
      ];
    } else px = BG;
    const off = rowStart + 1 + x * 4;
    raw[off] = px[0];
    raw[off + 1] = px[1];
    raw[off + 2] = px[2];
    raw[off + 3] = px[3];
  }
}

const ihdr = Buffer.alloc(13);
ihdr.writeUInt32BE(SIZE, 0);
ihdr.writeUInt32BE(SIZE, 4);
ihdr[8] = 8; // bit depth
ihdr[9] = 6; // color type RGBA
ihdr[10] = 0;
ihdr[11] = 0;
ihdr[12] = 0;

const png = Buffer.concat([
  Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
  chunk("IHDR", ihdr),
  chunk("IDAT", zlib.deflateSync(raw, { level: 9 })),
  chunk("IEND", Buffer.alloc(0)),
]);

const out = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "icon-source.png");
fs.writeFileSync(out, png);
console.log("written", out, png.length, "bytes");
