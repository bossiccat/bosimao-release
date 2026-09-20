#!/usr/bin/env node
/**
 * 有界执行器 —— 把「可能静默挂住」的外部命令变成「有界 + 响亮失败 + 可诊断」。
 *
 * 为什么存在
 * ----------
 * 2026-09-19 实测：`npm ci` 在本机构建链上**静默挂死 17 分钟**，期间 npm 自带
 * debug 日志停在 `13 silly idealTree buildDeps`，**一条 fetch 都没有**，
 * `node_modules` 与缓存目录近 20 分钟零写入。它挂住的方式是「看起来在跑，
 * 其实什么都没发生」—— 这正是我们这一天在消灭的那类现象。
 *
 * 而提案中的 windows-latest CI job 第 0 步就是 `npm ci`（5 件原生集由它提供）。
 * 若它会无限期静默挂住，那条 job 会一直烧到 GitHub 的默认 job 超时（数小时），
 * 且**没有任何输出能告诉你卡在哪**。
 *
 * 因此给它一个上界：超时 ⇒ 杀掉**整棵进程树** ⇒ 打印末尾 N 行 + 日志路径 ⇒
 * 退出码 124（与 coreutils `timeout(1)` 同码，便于在日志里一眼认出"这是超时，
 * 不是随机失败"）。非零退出同样带出末尾 N 行，避免"退出码有了、现场没了"。
 *
 * 用法
 * ----
 *   node scripts/bounded-exec.mjs --timeout-seconds 900 --log <文件>
 *        [--tail 60] [--cwd <目录>] [--shell] -- <命令> [参数...]
 *
 * `--` 之后的一切原样传给子进程。**不接受没有 `--` 的调用**：把选项与命令
 * 混在一段参数里，是这类小工具最典型的误用来源。
 *
 * 退出码
 * ------
 *   0..255 = 子进程自己的退出码（原样透传）
 *   124    = 超时（子进程树已被杀死）
 *   127    = 子进程根本没起来（ENOENT 等）
 *   2      = 本工具自身的用法错误
 *
 * 注意：进程树的杀法在 Windows 与 POSIX 上不同（Windows 用 `taskkill /T /F`，
 * POSIX 用进程组负号信号）。两条路径都在本机实测过，见
 * `bounded-exec.test.mjs`。
 */
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const EXIT_USAGE = 2;
const EXIT_TIMEOUT = 124;
const EXIT_START_FAILED = 127;

const USAGE =
  'usage: bounded-exec.mjs --timeout-seconds N --log FILE [--tail N] [--cwd DIR] [--shell] -- CMD [ARGS...]\n';

function die(msg) {
  process.stderr.write(`bounded-exec: ${msg}\n${USAGE}`);
  process.exit(EXIT_USAGE);
}

function parseArgv(argv) {
  const opts = { timeoutSeconds: null, log: null, tail: 60, cwd: process.cwd(), shell: false };
  let i = 0;
  for (; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === '--') {
      i += 1;
      break;
    }
    if (a === '--shell') {
      opts.shell = true;
    } else if (a === '--timeout-seconds') {
      opts.timeoutSeconds = Number(argv[i + 1]);
      i += 1;
    } else if (a === '--log') {
      opts.log = argv[i + 1];
      i += 1;
    } else if (a === '--tail') {
      opts.tail = Number(argv[i + 1]);
      i += 1;
    } else if (a === '--cwd') {
      opts.cwd = argv[i + 1];
      i += 1;
    } else {
      die(`unknown option: ${a}`);
    }
  }
  const command = argv.slice(i);
  if (command.length === 0) die('缺少 `--` 之后的命令');
  if (!Number.isFinite(opts.timeoutSeconds) || opts.timeoutSeconds <= 0) {
    die('--timeout-seconds 必须是正数');
  }
  if (!opts.log) die('--log 是必填（超时时必须能指出日志路径）');
  if (!Number.isInteger(opts.tail) || opts.tail <= 0) die('--tail 必须是正整数');
  return { ...opts, command };
}

/** 只保留末尾 n 行的环形缓冲：挂住的命令通常前面刷几千行，末尾才有线索。 */
function makeRing(n) {
  const buf = [];
  let total = 0;
  return {
    push(line) {
      total += 1;
      buf.push(line);
      if (buf.length > n) buf.shift();
    },
    tail: () => buf.slice(),
    get total() {
      return total;
    },
  };
}

function killTree(child) {
  if (child.exitCode !== null || child.signalCode !== null) return;
  if (process.platform === 'win32') {
    // 必须 /T。实测（见 bounded-exec.test.mjs 与 probe_grandchild.mjs）：
    // 非 detached 的后代会随父进程一起死，但 **detached** 的后代不会 ——
    // 只有 /T 才把它一起带走。而 npm 的 postinstall 脚本完全可能拉起 detached 进程。
    //
    // 用 spawnSync 而不是 spawn：`taskkill /T` 靠"父进程还活着"时才枚举得到的
    // 父子树。若先 spawn 它、再立刻 child.kill()，父进程可能先死，taskkill 醒来
    // 时树已经断了 —— 一个真实的竞态，会让 /T 时灵时不灵。
    try {
      spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore' });
    } catch {
      /* 下面还有兜底 */
    }
  } else {
    try {
      process.kill(-child.pid, 'SIGKILL');
    } catch {
      /* 兜底 */
    }
  }
  try {
    child.kill('SIGKILL');
  } catch {
    /* 已经死了 */
  }
}

function reportLoud(reason, ring, logPath, elapsedMs) {
  const lines = ring.tail();
  process.stderr.write(
    `\n${'-'.repeat(70)}\n` +
      `!! bounded-exec: ${reason}\n` +
      `!! 这不是随机失败。子进程已被杀死，下面把现场带出来。\n` +
      `!! 完整日志: ${logPath}\n` +
      `!! 已运行: ${(elapsedMs / 1000).toFixed(1)}s；输出共 ${ring.total} 行，仅显示末尾 ${lines.length} 行\n` +
      `${'-'.repeat(70)}\n`
  );
  for (const l of lines) process.stderr.write(`${l}\n`);
  process.stderr.write(`${'-'.repeat(70)}\n`);
}

const opts = parseArgv(process.argv.slice(2));
const [cmd, ...cmdArgs] = opts.command;

fs.mkdirSync(path.dirname(path.resolve(opts.log)), { recursive: true });
const logStream = fs.createWriteStream(opts.log, { flags: 'a' });
logStream.write(
  `\n===== bounded-exec ${new Date().toISOString()} timeout=${opts.timeoutSeconds}s ` +
    `shell=${opts.shell} cwd=${opts.cwd} cmd=${opts.command.join(' ')} =====\n`
);
// 同步再写一份到 stderr 头部，保证 CI 日志里能看到边界参数（不依赖 artifact）。
process.stderr.write(
  `bounded-exec: timeout=${opts.timeoutSeconds}s shell=${opts.shell} log=${opts.log}\n`
);

const ring = makeRing(opts.tail);
const child = spawn(cmd, cmdArgs, {
  cwd: opts.cwd,
  shell: opts.shell,
  // POSIX 下 detached 才拿得到独立进程组，kill(-pid) 才能一次杀干净。
  // Windows 下 detached 会让子进程脱离控制台，反而让 taskkill /T 之外的手段失效，故不开。
  detached: process.platform !== 'win32',
  stdio: ['ignore', 'pipe', 'pipe'],
  windowsHide: true,
});

const startedAt = Date.now();

function pipe(stream, tag) {
  let partial = '';
  stream.on('data', (chunk) => {
    partial += String(chunk);
    const lines = partial.split(/\r?\n/);
    partial = lines.pop() ?? ''; // 最后一段可能是半行，留到下一块
    for (const line of lines) {
      logStream.write(`[${tag}] ${line}\n`);
      ring.push(`[${tag}] ${line}`);
    }
  });
  stream.on('end', () => {
    if (partial) {
      logStream.write(`[${tag}] ${partial}\n`);
      ring.push(`[${tag}] ${partial}`);
    }
  });
}
pipe(child.stdout, 'out');
pipe(child.stderr, 'err');

let timedOut = false;
const timer = setTimeout(() => {
  timedOut = true;
  const elapsed = Date.now() - startedAt;
  killTree(child);
  // 给 taskkill / 信号一点时间落地，再把现场写出来（否则末尾几行可能还没 flush）
  setTimeout(() => {
    reportLoud(
      `超时：子进程在 ${opts.timeoutSeconds}s 内没有结束 —— 判定为"静默挂住"`,
      ring,
      opts.log,
      elapsed
    );
    logStream.end(() => process.exit(EXIT_TIMEOUT));
  }, 1500);
}, opts.timeoutSeconds * 1000);

child.on('error', (err) => {
  clearTimeout(timer);
  process.stderr.write(`bounded-exec: 子进程无法启动: ${err.message}\n`);
  logStream.end(() => process.exit(EXIT_START_FAILED));
});

child.on('close', (code, signal) => {
  if (timedOut) return; // 超时路径自己收尾
  clearTimeout(timer);
  const elapsed = Date.now() - startedAt;
  if (code !== 0) {
    reportLoud(
      `子进程失败（code=${code} signal=${signal}）`,
      ring,
      opts.log,
      elapsed
    );
  } else {
    process.stderr.write(
      `bounded-exec: OK（${(elapsed / 1000).toFixed(1)}s，${ring.total} 行）log=${opts.log}\n`
    );
  }
  logStream.end(() => process.exit(code === null ? 1 : code));
});
