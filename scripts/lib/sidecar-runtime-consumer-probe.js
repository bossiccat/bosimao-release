'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const DIAGNOSTIC = 'SIDECAR_RUNTIME_MIGRATION_CONSUMER_PROBE_';
const POWERSHELL_SCRIPT = [
  '$ErrorActionPreference = "Stop"',
  '$processes = @(Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine)',
  '$ports = @(Get-NetTCPConnection -State Listen | Select-Object LocalPort,OwningProcess)',
  '[PSCustomObject]@{ processes = $processes; ports = $ports } | ConvertTo-Json -Compress -Depth 5',
].join('; ');
const POWERSHELL_ARGS = ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', POWERSHELL_SCRIPT];

function diagnostic(code) {
  return [{ diagnostic: `${DIAGNOSTIC}${code}` }];
}

function defaultRun(command, args) {
  // 全量 Win32_Process（含 ExecutablePath/CommandLine）+ Get-NetTCPConnection 在
  // 真实机器上常需 5-30s，输出可达数十 MB；过小的超时/缓冲会把"机器慢"误判为
  // runner 不可用，从而错误阻断迁移。
  return spawnSync(command, args, {
    encoding: 'utf8',
    windowsHide: true,
    timeout: 60000,
    maxBuffer: 64 * 1024 * 1024,
  });
}

function canonical(value) {
  return path.win32.normalize(String(value).replace(/\//g, '\\')).replace(/[\\]+$/, '').toLowerCase();
}

function basename(value) {
  return path.win32.basename(canonical(value));
}

function isWithin(candidate, root) {
  const c = canonical(candidate);
  const r = canonical(root);
  return c === r || c.startsWith(`${r}\\`);
}

function generationRoots(runtimeDir, processes) {
  const root = canonical(runtimeDir);
  const roots = new Set();
  for (const process of processes) {
    const executable = process.executablePath;
    if (typeof executable !== 'string') continue;
    const normalized = canonical(executable);
    const marker = `${root}\\generations\\`;
    if (normalized.startsWith(marker)) {
      const remainder = normalized.slice(marker.length);
      const generation = remainder.split('\\')[0];
      if (generation) roots.add(`${marker}${generation}`);
    }
  }
  return roots;
}

function parseSnapshot(raw) {
  if (raw && typeof raw === 'object' && !Buffer.isBuffer(raw)) return raw;
  if (typeof raw !== 'string' || raw.trim() === '') throw new Error('empty output');
  return JSON.parse(raw);
}

function normalizeSnapshot(snapshot) {
  if (!snapshot || typeof snapshot !== 'object' || Array.isArray(snapshot)
    || !Array.isArray(snapshot.processes) || !Array.isArray(snapshot.ports)) {
    throw new Error('invalid snapshot shape');
  }
  const processes = snapshot.processes.map((item) => {
    if (!item || typeof item !== 'object') throw new Error('invalid process');
    const pid = item.pid ?? item.ProcessId;
    const parentProcessId = item.parentProcessId ?? item.ParentProcessId;
    const name = item.name ?? item.Name;
    const executablePath = item.executablePath ?? item.ExecutablePath;
    const commandLine = item.commandLine ?? item.CommandLine;
    // 真实机器上必然存在 ExecutablePath/CommandLine 为 null 的系统进程
    // （System Idle Process、Registry、受保护的 svchost 等）。这不是脏输出：
    // pid/parent/name 结构合法即保留，缺路径/命令行的记录随后无法归因、
    // 自然跳过。只有结构垃圾（pid 非法等）才判定整份快照无效（fail-closed）。
    // pid<=0 是真实的内核级记录（System Idle Process 等），永远不可能是
    // consumer：以空路径/命令行保留，后续归因循环自然跳过。只有结构垃圾
    // （pid 非整数、父 PID 非法）才判定整份快照无效（fail-closed）。
    if (!Number.isInteger(pid) || !Number.isInteger(parentProcessId) || parentProcessId < 0
      || typeof name !== 'string' || !name.trim()) throw new Error('invalid process');
    if (pid <= 0 || typeof executablePath !== 'string' || !executablePath.trim()
      || typeof commandLine !== 'string' || !commandLine.trim()) {
      return { pid, parentProcessId, name, executablePath: null, commandLine: null };
    }
    return { pid, parentProcessId, name, executablePath, commandLine };
  });
  const ports = snapshot.ports.map((item) => {
    if (!item || typeof item !== 'object') throw new Error('invalid port');
    const owningPid = item.owningPid ?? item.OwningProcess;
    const port = item.port ?? item.LocalPort;
    if (!Number.isInteger(owningPid) || owningPid <= 0 || !Number.isInteger(port) || port <= 0) throw new Error('invalid port');
    return { owningPid, port };
  });
  return { processes, ports };
}

function inspectConsumers(runtimeDir, options = {}) {
  if (typeof runtimeDir !== 'string' || !runtimeDir.trim()) return diagnostic('RUNTIME_INVALID');
  const platform = options.platform || process.platform;
  if (platform !== 'win32') return [];

  let snapshot;
  try {
    if (typeof options.runner === 'function') {
      snapshot = options.runner();
    } else {
      const run = options.run || defaultRun;
      const result = run('powershell.exe', POWERSHELL_ARGS);
      if (!result || result.error) return diagnostic('RUNNER_UNAVAILABLE');
      if (result.status !== 0) return diagnostic('RUNNER_NONZERO');
      snapshot = parseSnapshot(result.stdout ?? result.output);
    }
    snapshot = normalizeSnapshot(parseSnapshot(snapshot));
  } catch {
    return diagnostic('SNAPSHOT_INVALID');
  }
  if (snapshot.processes.length === 0) return diagnostic('PROCESS_INVENTORY_EMPTY');

  const realpath = options.realpath || fs.realpathSync;
  let resolvedRuntimeDir;
  try {
    resolvedRuntimeDir = realpath(runtimeDir);
    if (typeof resolvedRuntimeDir !== 'string' || !resolvedRuntimeDir.trim()) return diagnostic('REALPATH_FAILED');
  } catch {
    return diagnostic('REALPATH_FAILED');
  }
  const runtimeRoot = canonical(resolvedRuntimeDir);
  const roots = generationRoots(resolvedRuntimeDir, snapshot.processes);
  const records = [];
  const byPid = new Map();
  let invalidPath = false;
  for (const process of snapshot.processes) {
    // 缺路径/命令行的合法系统进程无法归因，直接跳过（不是快照无效）。
    if (typeof process.executablePath !== 'string' || !process.executablePath.trim()
      || typeof process.commandLine !== 'string' || !process.commandLine.trim()) continue;
    let executable;
    try {
      executable = realpath(process.executablePath);
      if (typeof executable !== 'string' || !executable.trim()) return diagnostic('REALPATH_FAILED');
    } catch {
      return diagnostic('REALPATH_FAILED');
    }
    const executableCanonical = canonical(executable);
    const processName = basename(executableCanonical) || process.name.toLowerCase();
    const inGeneration = [...roots].some((root) => isWithin(executableCanonical, root));
    const inLegacyRoot = isWithin(executableCanonical, runtimeRoot);
    const commandLine = canonical(process.commandLine);
    const commandMentionsRuntime = commandLine.includes(runtimeRoot);
    const runtimeArgument = (commandLine.includes('--runtime-dir') || commandLine.includes('--sidecar-runtime'))
      && commandMentionsRuntime;
    const sidecarLike = processName === 'jax-rtc-sidecar.exe'
      || processName === 'electron.exe' || processName === 'electron';
    const inExpectedRuntime = inGeneration || inLegacyRoot;
    if (commandMentionsRuntime && sidecarLike && !inExpectedRuntime) invalidPath = true;
    const direct = (
      (processName === 'jax-pet.exe' && runtimeArgument)
      || (processName === 'jax-rtc-sidecar.exe' && inExpectedRuntime && (runtimeArgument || commandLine.includes('--role=sidecar')))
      || ((processName === 'electron.exe' || processName === 'electron') && inExpectedRuntime && commandMentionsRuntime)
    );
    byPid.set(process.pid, { process, executable, executableCanonical, inExpectedRuntime, direct });
  }
  if (invalidPath) return diagnostic('PATH_OUTSIDE_GENERATION');

  const attributed = new Set();
  const queue = [...byPid.values()].filter((entry) => entry.direct);
  while (queue.length) {
    const entry = queue.shift();
    if (attributed.has(entry.process.pid)) continue;
    attributed.add(entry.process.pid);
    for (const child of byPid.values()) {
      if (child.process.parentProcessId === entry.process.pid && child.inExpectedRuntime && !attributed.has(child.process.pid)) queue.push(child);
    }
  }

  for (const pid of attributed) {
    const entry = byPid.get(pid);
    const evidence = [entry.direct ? 'process-identity' : 'attributed-descendant'];
    const ownedPorts = snapshot.ports.filter((port) => port.owningPid === pid).map((port) => port.port);
    if (ownedPorts.length) evidence.push(`listen-port:${ownedPorts.join(',')}`);
    records.push({
      pid,
      parentProcessId: entry.process.parentProcessId,
      name: entry.process.name,
      executablePath: entry.executable,
      commandLine: entry.process.commandLine,
      evidence,
    });
  }
  return records.sort((a, b) => a.pid - b.pid);
}

module.exports = { inspectConsumers };
