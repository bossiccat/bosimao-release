/**
 * 独立复核扫描器（verifier 工具，非产品代码）
 * 覆盖规格第 7 节验收标准 #4 与第 6 节工程约束：
 *   - 组件内 emoji 图标计数（应为 0）
 *   - 非 token 硬编码色值计数（排除 #fff/#000 与设计 token 定义文件，应为 0）
 *   - 运行时内联 <style> 标签计数（应为 0）—— 仅统计真正渲染的标签，注释/字符串中的 <style 不算
 *   - 各源文件行数是否 ≤300（§6 文件行数契约，区分 JS/TS 模块与样式表）
 *   - 规格 §2 要求的 pet 配色 token 是否全部进入 design-tokens.css
 * 用法：node verify-acceptance.mjs [dir]  （默认 pet-ui/src）
 */
import fs from "node:fs";
import path from "node:path";

const ROOT = process.argv[2] || path.resolve("src");
const ALLOWED_HEX = new Set(["#fff", "#ffffff", "#000", "#000000"]);
const TOKEN_DEF_FILES = new Set(["design-tokens.css", "design-tokens.json"]);

function walk(dir, acc = []) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) walk(p, acc);
    else acc.push(p);
  }
  return acc;
}

// 仅剥离注释（用于色值扫描：保留 JSX 属性字符串如 fill="#xxx" 以便检测违规硬编码色）
function stripComments(src) {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, " ") // 块注释
    .replace(/\/\/[^\n]*/g, " "); // 行注释
}
// 剥离注释 + 字符串字面量（用于 <style>/emoji 扫描：排除测试字符串与注释里的误报）
function stripAll(src) {
  return stripComments(src)
    .replace(/`(?:[^`\\]|\\.)*`/g, "`") // 模板串
    .replace(/"(?:[^"\\]|\\.)*"/g, '"') // 双引号串
    .replace(/'(?:[^'\\]|\\.)*'/g, "'"); // 单引号串
}

function trueLineCount(src) {
  let n = src.split("\n").length;
  if (src.endsWith("\n")) n -= 1;
  return n;
}

const files = walk(ROOT).filter((f) => /\.(tsx?|css)$/.test(f));

// 真实 emoji 图形（排除箭头/技术符号/方框绘制等，它们不是"图标 emoji"）
const EMOJI_RE = /[\u{1F000}-\u{1FAFF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}\u{1F1E6}-\u{1F1FF}\u{FE0F}]/u;
const HEX_RE = /#([0-9a-fA-F]{3,8})\b/g;
const STYLE_TAG_RE = /<style[\s>\/]/;

const emojiHits = [];
const colorHits = [];
const styleTagHits = [];
const overLimitJS = [];
const overLimitCSS = [];

for (const f of files) {
  const rel = path.relative(ROOT, f);
  const base = path.basename(f);
  const raw = fs.readFileSync(f, "utf8");
  const code = stripAll(raw); // 用于 <style>/emoji
  const codeColor = stripComments(raw); // 用于色值（保留属性字符串）
  const lines = trueLineCount(raw);
  const isCss = f.endsWith(".css");

  if (lines > 300) (isCss ? overLimitCSS : overLimitJS).push({ rel, lines });

  if (EMOJI_RE.test(code)) {
    code.split("\n").forEach((ln, i) => {
      if (EMOJI_RE.test(ln)) emojiHits.push({ rel, line: i + 1, text: ln.trim().slice(0, 80) });
    });
  }
  if (STYLE_TAG_RE.test(code)) styleTagHits.push({ rel });

  const isTokenDef = TOKEN_DEF_FILES.has(base);
  let m;
  HEX_RE.lastIndex = 0;
  while ((m = HEX_RE.exec(codeColor)) !== null) {
    const hex = "#" + m[1].toLowerCase();
    if (ALLOWED_HEX.has(hex)) continue;
    if (isTokenDef) continue;
    colorHits.push({ rel, hex });
  }
}

// §2 pet 配色 token 是否全部进入 design-tokens.css
const dtPath = path.join(ROOT, "styles/design-tokens.css");
const dtSrc = fs.existsSync(dtPath) ? fs.readFileSync(dtPath, "utf8") : "";
const PET_TOKENS = [
  "--pet-fur-base", "--pet-fur-light", "--pet-fur-shade", "--pet-fur-deep",
  "--pet-outline", "--pet-inner", "--pet-nose", "--pet-eye",
  "--pet-collar", "--pet-tag", "--accent",
];
const missingTokens = PET_TOKENS.filter((t) => !dtSrc.includes(`${t}:`));

console.log("=== 验收扫描结果 ===");
console.log(`扫描目录: ${ROOT}`);
console.log(`源文件数: ${files.length}`);
console.log("");
console.log(`[emoji 图标] 命中: ${emojiHits.length}`);
emojiHits.forEach((h) => console.log(`  - ${h.rel}:${h.line}  ${h.text}`));
console.log("");
console.log(`[非 token 硬编码色值] 命中: ${colorHits.length}`);
[...new Set(colorHits.map((c) => `${c.rel}  ${c.hex}`))].forEach((c) => console.log(`  - ${c}`));
console.log("");
console.log(`[运行时内联 <style> 标签] 命中: ${styleTagHits.length}`);
styleTagHits.forEach((h) => console.log(`  - ${h.rel}`));
console.log("");
console.log(`[JS/TS 模块 >300 行] 命中: ${overLimitJS.length}`);
overLimitJS.forEach((h) => console.log(`  - ${h.rel}: ${h.lines} 行`));
console.log(`[CSS 样式表 >300 行] 命中: ${overLimitCSS.length}（§6 契约原文限定 "JavaScript modules"，样式表是否同约束见报告）`);
overLimitCSS.forEach((h) => console.log(`  - ${h.rel}: ${h.lines} 行`));
console.log("");
console.log(`[§2 pet 配色 token 缺失] 命中: ${missingTokens.length}`);
missingTokens.forEach((t) => console.log(`  - ${t}`));

const verdict =
  emojiHits.length === 0 &&
  colorHits.length === 0 &&
  styleTagHits.length === 0 &&
  overLimitJS.length === 0 &&
  missingTokens.length === 0;
console.log("");
console.log(verdict ? "VERDICT: PASS (全部扫描通过)" : "VERDICT: FAIL (见上方命中项)");
process.exit(verdict ? 0 : 1);
