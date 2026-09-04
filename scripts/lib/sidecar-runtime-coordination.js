'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const LOCK_SUFFIX = '.coordination-lock';

// 同步脚本无法持有 Rust helper 的租约：sidecar-publish-coordination 以行式
// stdin 循环运行，租约存活在 helper 进程内，spawnSync 一次性调用会在进程退出
// 时立即释放互斥量，从而产生“假互斥”。因此在同步发布/迁移路径上改用**单文件
// 跨进程租约**：owner 记录本身即锁文件，`wx` 独占创建提供原子性，释放只需要
// unlinkSync——不依赖 mkdir/rmdir（目录删除在受限环境可能被删除 shim 拦截挂起）。
// owner 身份字段与 Rust helper 的 Owner 保持一致，便于后续切换。
function fail(code) {
  const error = new Error(code);
  error.code = code;
  throw error;
}

function canonicalPath(value) {
  return path.normalize(String(value || '')).replace(/[\\/]+$/, '').toLowerCase();
}

function runtimeCoordinationIdentity(runtimeDir, options = {}) {
  const runtime = path.normalize(runtimeDir).replace(/[\\/]+$/, '');
  const parent = path.dirname(runtime);
  const runtimeName = path.basename(runtime);
  if (!parent || parent === runtime || !runtimeName) return '';
  // 必须先解析父目录：Windows 上同一目录可用 8.3 短名（ADMINI~1）或长名
  // 访问，仅做小写化会得到两个不同的锁名，从而绕过互斥。普通 realpathSync
  // 不展开 8.3 短名，必须用 native。
  const defaultRealpath = process.platform === 'win32' && typeof fs.realpathSync.native === 'function'
    ? fs.realpathSync.native
    : fs.realpathSync;
  let resolvedParent = parent;
  try {
    resolvedParent = (options.realpath || defaultRealpath)(parent);
  } catch {
    resolvedParent = parent;
  }
  return canonicalPath(path.join(resolvedParent, runtimeName));
}

function coordinationLockPath(runtimeDir, options = {}) {
  return `${runtimeCoordinationIdentity(runtimeDir, options)}${LOCK_SUFFIX}`;
}

function isUuid(value) {
  return typeof value === 'string'
    && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
}

function isRfc3339(value) {
  return typeof value === 'string' && !Number.isNaN(Date.parse(value))
    && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/.test(value);
}

function readOwner(lockFile) {
  if (!fs.existsSync(lockFile)) return null;
  let parsed;
  try {
    parsed = JSON.parse(fs.readFileSync(lockFile, 'utf8') || '{}');
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== 'object') return null;
  if (parsed.schema_version !== 1
    || !isUuid(parsed.token)
    || !Number.isInteger(parsed.pid) || parsed.pid <= 0
    || !isRfc3339(parsed.created_at)
    || !isRfc3339(parsed.process_creation_time)
    || typeof parsed.process_creation_identity !== 'string'
    || parsed.process_creation_identity.length === 0) return null;
  return parsed;
}

function inspectProcessWindows(pid, options = {}) {
  if (!Number.isInteger(pid) || pid <= 0) return { status: 'unknown' };
  const script = `$p = Get-CimInstance -ClassName Win32_Process -Filter 'ProcessId = ${pid}';`
    + ' if ($null -eq $p) { \'{"status":"absent"}\' }'
    + ' else { ConvertTo-Json -Compress @{ status = \'alive\'; pid = $p.ProcessId;'
    + ' creationTime = (Get-Date $p.CreationDate).ToUniversalTime().ToString(\'o\') } }';
  const runner = options.runPowerShell
    || ((command, args) => {
      // eslint-disable-next-line global-require
      const { spawnSync } = require('node:child_process');
      return spawnSync(command, args, { encoding: 'utf8', windowsHide: true, timeout: 5000 });
    });
  let result;
  try {
    result = runner('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script]);
  } catch {
    return { status: 'unknown' };
  }
  if (!result || result.error || result.status !== 0 || typeof result.stdout !== 'string') {
    return { status: 'unknown' };
  }
  try {
    const parsed = JSON.parse(result.stdout.trim());
    if (parsed && (parsed.status === 'alive' || parsed.status === 'absent')) return parsed;
    return { status: 'unknown' };
  } catch {
    return { status: 'unknown' };
  }
}

function sameInstant(left, right) {
  const a = Date.parse(left);
  const b = Date.parse(right);
  if (Number.isNaN(a) || Number.isNaN(b)) return false;
  // Win32 CreationDate 精度低于 ISO 毫秒串，允许 2s 容差判定同一进程实例。
  return Math.abs(a - b) <= 2000;
}

function acquireRuntimeLease(runtimeDir, options = {}) {
  const identity = runtimeCoordinationIdentity(runtimeDir, options);
  if (!identity) fail('SIDECAR_RUNTIME_COORDINATION_RUNTIME_INVALID');

  const runtime = path.normalize(runtimeDir).replace(/[\\/]+$/, '');
  const runtimeName = path.basename(runtime);
  const lockFile = coordinationLockPath(runtimeDir, options);
  const inspectProcess = options.inspectProcess
    || ((pid) => inspectProcessWindows(pid, options));

  let fd;
  try {
    fd = fs.openSync(lockFile, 'wx');
  } catch (error) {
    if (error && error.code === 'EEXIST') {
      const owner = readOwner(lockFile);
      if (!owner) fail('SIDECAR_RUNTIME_COORDINATION_OWNER_AMBIGUOUS');
      const probe = inspectProcess(owner.pid);
      if (!probe || probe.status === 'unknown') fail('SIDECAR_RUNTIME_COORDINATION_PROBE_UNAVAILABLE');
      if (probe.status === 'absent') fail('SIDECAR_RUNTIME_COORDINATION_STALE_OWNER');
      if (typeof probe.creationTime === 'string' && probe.creationTime
        && !sameInstant(probe.creationTime, owner.process_creation_time)) {
        fail('SIDECAR_RUNTIME_COORDINATION_PID_REUSED');
      }
      fail('SIDECAR_RUNTIME_COORDINATION_BUSY');
    }
    if (error && (error.code === 'ENOENT' || error.code === 'ENOTDIR')) {
      fail('SIDECAR_RUNTIME_COORDINATION_RUNTIME_PARENT_MISSING');
    }
    throw error;
  }

  const record = {
    schema_version: 1,
    token: options.token || crypto.randomUUID(),
    pid: options.pid || process.pid,
    created_at: options.createdAt || new Date().toISOString(),
    process_creation_time: options.processCreationTime || new Date().toISOString(),
    process_creation_identity: options.processCreationIdentity
      || `${process.platform}:${process.arch}:${process.version}`,
    operation: options.operation === 'migration' ? 'migration' : 'publish',
    runtime_name: runtimeName,
  };
  try {
    fs.writeFileSync(fd, `${JSON.stringify(record)}\n`);
  } finally {
    fs.closeSync(fd);
  }

  let released = false;
  return () => {
    if (released) return;
    released = true;
    const current = readOwner(lockFile);
    if (!current || current.token !== record.token) {
      fail('SIDECAR_RUNTIME_COORDINATION_LEASE_LOST');
    }
    // 显式失败：释放失败必须抛出稳定诊断，绝不能静默留下锁文件，
    // 否则后续发布/迁移会被自己的残留锁永久阻断。
    // 瞬态错误（AV/索引器瞬时占用锁文件 → EBUSY/EPERM/EACCES/EAGAIN）做
    // 有界重试：锁此时仍归本进程所有（上方 token 已核验），重试语义安全；
    // 非瞬态错误保持立即失败。耗尽后抛 RELEASE_FAILED 并保留最后 errno
    // 与尝试次数（此前 catch 吞 errno 导致间歇性 RELEASE_FAILED 无法归因）。
    const transientCodes = new Set(['EBUSY', 'EPERM', 'EACCES', 'EAGAIN']);
    const maxAttempts = 5;
    const delayMs = typeof options.retryDelayMs === 'number' ? options.retryDelayMs : 100;
    for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
      try {
        (options.unlink || fs.unlinkSync)(lockFile);
        return;
      } catch (error) {
        const lastErrnoCode = (error && error.code) || '';
        const transient = transientCodes.has(lastErrnoCode);
        if (!transient || attempt === maxAttempts) {
          const releaseError = new Error('SIDECAR_RUNTIME_COORDINATION_RELEASE_FAILED');
          releaseError.code = 'SIDECAR_RUNTIME_COORDINATION_RELEASE_FAILED';
          releaseError.last_errno_code = lastErrnoCode || 'UNKNOWN';
          releaseError.attempts = attempt;
          throw releaseError;
        }
        if (delayMs > 0) {
          const shared = new Int32Array(new SharedArrayBuffer(4));
          Atomics.wait(shared, 0, 0, delayMs);
        }
      }
    }
  };
}

module.exports = {
  acquireRuntimeLease,
  coordinationLockPath,
  runtimeCoordinationIdentity,
};
