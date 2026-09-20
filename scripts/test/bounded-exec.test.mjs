// bounded-exec.mjs 的行为测试。跑法：node --test bounded-exec.test.mjs
//
// 这里每条断言都对应"有界执行"必须给出的一个性质，而不是"代码没崩"：
//   1. 正常命令 → 原样透传退出码，输出落进日志
//   2. 失败命令 → 非零码 + 末尾输出被带出来（不是只有退出码、没有现场）
//   3. 挂住的命令 → 有界终止（124）+ 整棵进程树死透（含**孙进程**）
//   4. 用法错误 → 2（而不是静默跑一个没超时的命令）
//   5. --shell 路由真的能拉起 .cmd 形态的命令（Windows 上 npm 就是这种）
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
// 本文件住在 scripts/test/，被测对象在 scripts/ —— 上跳一层。
const RUNNER = path.resolve(HERE, '..', 'bounded-exec.mjs');

// 夹具是**运行时写出来的**，不额外占一个仓库文件：
// 三层进程（自己 + 非 detached 子孙 + **detached 子孙**）各写心跳，
// 靠心跳是否继续增长判断"到底谁还活着"。detached 那个是断言的关键，见测试 3 的注释。
const HANG_FIXTURE = `import fs from 'node:fs';
import { spawn } from 'node:child_process';
const [hb, hbChild, hbGrand] = process.argv.slice(2);
let n = 0;
setInterval(() => { fs.writeFileSync(hb, String(++n)); }, 200);
const code = (t) =>
  'const fs=require("fs");let m=0;' +
  \`setInterval(()=>fs.writeFileSync(\${JSON.stringify(t)},String(++m)),200);\` +
  'setInterval(()=>{},1000);';
if (hbChild) spawn(process.execPath, ['-e', code(hbChild)], { stdio: 'ignore' });
if (hbGrand) spawn(process.execPath, ['-e', code(hbGrand)], { stdio: 'ignore', detached: true });
setInterval(() => {}, 1000);
`;

function tmpdir(tag) {
  return fs.mkdtempSync(path.join(os.tmpdir(), `bounded-exec-${tag}-`));
}

function writeHangFixture(dir) {
  const p = path.join(dir, 'hang-fixture.mjs');
  fs.writeFileSync(p, HANG_FIXTURE, 'utf8');
  return p;
}

function run(args, opts = {}) {
  return spawnSync(process.execPath, [RUNNER, ...args], {
    encoding: 'utf8',
    timeout: 60000,
    ...opts,
  });
}

test('正常命令：退出码原样透传，输出落进日志', () => {
  const d = tmpdir('ok');
  const log = path.join(d, 'run.log');
  const r = run([
    '--timeout-seconds', '20',
    '--log', log,
    '--',
    process.execPath, '-e', 'console.log("hello-bounded")',
  ]);
  assert.equal(r.status, 0, `stderr=${r.stderr}`);
  assert.ok(r.stderr.includes('OK'), `stderr 应报告 OK，实为: ${r.stderr}`);
  const text = fs.readFileSync(log, 'utf8');
  assert.ok(text.includes('hello-bounded'), `日志缺输出: ${text}`);
  // 日志头部必须写清边界参数，否则事后无法判断"当时给的是多少秒"
  assert.ok(text.includes('timeout=20s'), `日志缺边界参数: ${text}`);
});

test('失败命令：非零码 + 末尾输出被带出来', () => {
  const d = tmpdir('fail');
  const log = path.join(d, 'run.log');
  const r = run([
    '--timeout-seconds', '20',
    '--log', log,
    '--',
    process.execPath, '-e', 'console.error("boom-bounded"); process.exit(3)',
  ]);
  assert.equal(r.status, 3, `应透传子进程退出码 3，实为 ${r.status}`);
  assert.ok(r.stderr.includes('boom-bounded'), `末尾输出没被带出来: ${r.stderr}`);
  assert.ok(r.stderr.includes('子进程失败'), `缺少响亮失败块: ${r.stderr}`);
  assert.ok(r.stderr.includes(log), `响亮失败块必须指出日志路径: ${r.stderr}`);
});

test('挂住的命令：有界终止(124) 且整棵进程树死透（含 detached 孙进程）', async () => {
  // 夹具为什么必须是 **detached** 孙进程：实测（probe_grandchild.mjs）Windows 上
  // Node 非 detached 的后代会随父进程一起死 ⇒ 那种夹具下"只杀直接子进程"与
  // "杀整棵树"行为相同，断言恒真 = 假绿。第一版就是栽在这里：把 killTree 里
  // 的 /T 删掉，这条测试照样全绿。换成 detached 孙进程后，删掉 /T 才会红。
  const d = tmpdir('timeout');
  const log = path.join(d, 'run.log');
  const hbParent = path.join(d, 'hb-parent.txt');
  const hbChild = path.join(d, 'hb-child.txt');
  const hang = writeHangFixture(d);

  const r = run(
    [
      '--timeout-seconds', '2',
      '--tail', '5',
      '--log', log,
      '--',
      process.execPath, hang, hbParent, hbChild,
    ],
    { timeout: 60000 }
  );

  assert.equal(r.status, 124, `超时必须走 124，实为 ${r.status}; stderr=${r.stderr}`);
  assert.ok(r.stderr.includes('超时'), `缺少超时说明: ${r.stderr}`);
  assert.ok(r.stderr.includes('不是随机失败'), `必须点明"这不是随机失败": ${r.stderr}`);
  assert.ok(r.stderr.includes(log), `必须指出日志路径: ${r.stderr}`);

  // 关键断言：进程树真的死了 —— 直接子进程与它那个 detached 子进程都停止增长。
  const read = (p) => (fs.existsSync(p) ? Number(fs.readFileSync(p, 'utf8')) : 0);
  const p1 = read(hbParent); // 直接子进程（夹具本体）
  const c1 = read(hbChild);  // 它的 detached 子进程（= bounded-exec 视角的孙进程）
  assert.ok(p1 > 0, '直接子进程心跳从未写入，测试前提不成立');
  assert.ok(c1 > 0, 'detached 孙进程心跳从未写入，测试前提不成立');
  await new Promise((res) => setTimeout(res, 1500));
  const p2 = read(hbParent);
  const c2 = read(hbChild);
  assert.equal(p2, p1, `父进程仍在跑（心跳 ${p1} -> ${p2}）：没有被杀干净`);
  assert.equal(c2, c1, `detached 孙进程仍在跑（心跳 ${c1} -> ${c2}）：/T 没生效，树没杀掉`);
});

test('用法错误：缺 `--` 必须退出 2，而不是跑一个没有超时的命令', () => {
  const d = tmpdir('usage');
  const log = path.join(d, 'run.log');
  const r = run(['--timeout-seconds', '5', '--log', log, process.execPath, '-e', '0']);
  assert.equal(r.status, 2, `应为用法错误 2，实为 ${r.status}`);
  assert.ok(r.stderr.includes('usage'), `应打印 usage: ${r.stderr}`);

  const r2 = run(['--log', log, '--', process.execPath, '-e', '0']);
  assert.equal(r2.status, 2, '缺 --timeout-seconds 也必须拒绝（默认无上界=违反本工具存在的理由）');
});

test('--shell 能拉起 .cmd 形态的命令（Windows 上 npm 就是这种）', { skip: process.platform !== 'win32' }, () => {
  const d = tmpdir('shell');
  const log = path.join(d, 'run.log');
  const r = run([
    '--timeout-seconds', '60',
    '--log', log,
    '--shell',
    '--',
    'npm', '--version',
  ]);
  assert.equal(r.status, 0, `npm --version 应成功，stderr=${r.stderr}`);
  assert.ok(r.stderr.includes('OK'), `stderr 应报告 OK: ${r.stderr}`);
  const text = fs.readFileSync(log, 'utf8');
  assert.match(text, /\d+\.\d+\.\d+/, `日志里应能看到 npm 版本号: ${text}`);
});
