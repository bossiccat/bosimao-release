'use strict';
// Task #46 dynamic gate: real multi-process contention, crash recovery
// (abandoned mutex + dead-owner reclaim), and cross-root isolation.

const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');

const EXE_DIR = 'C:/Users/Administrator/WorkBuddy/监视app/tools/sidecar-publish-coordination/target/debug';
const EXE = path.join(EXE_DIR, 'sidecar-publish-coordination.exe');

const failures = [];
function check(name, condition, detail) {
  console.log(`${condition ? 'PASS' : 'FAIL'} ${name}${condition ? '' : ' ' + (detail || '')}`);
  if (!condition) failures.push(name);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function startSidecar() {
  const proc = spawn(EXE, [], { stdio: ['pipe', 'pipe', 'pipe'] });
  const responses = [];
  let buffer = '';
  proc.stdout.on('data', (chunk) => {
    buffer += chunk.toString('utf8');
    let idx;
    while ((idx = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, idx).trim();
      buffer = buffer.slice(idx + 1);
      if (line) {
        try {
          responses.push(JSON.parse(line));
        } catch (e) {
          responses.push({ parse_error: line });
        }
      }
    }
  });
  proc.stderr.on('data', (c) => process.stderr.write(c));

  async function waitFor(predicate, timeoutMs) {
    const limit = timeoutMs || 15000;
    const start = responses.length;
    const deadline = Date.now() + limit;
    for (;;) {
      const hit = responses.slice(start).find(predicate);
      if (hit) return hit;
      if (Date.now() > deadline) throw new Error('timeout waiting for response');
      await sleep(25);
    }
  }

  function sendAcquire(root) {
    proc.stdin.write(
      JSON.stringify({
        operation: 'acquire',
        runtime_root: root,
        owner: {
          schema_version: 1,
          token: crypto.randomUUID(),
          pid: proc.pid,
          created_at: new Date().toISOString().replace(/\.\d{3}Z$/, '.000Z'),
          process_creation_time: '1970-01-01T00:00:00.000Z',
          process_creation_identity: 'e2e-multiproc',
        },
        timeout_ms: 5000,
      }) + '\n'
    );
  }

  function sendRelease(leaseId, token) {
    proc.stdin.write(
      JSON.stringify({
        operation: 'release',
        lease_id: leaseId,
        expected_token: token,
      }) + '\n'
    );
  }

  return { proc, waitFor, sendAcquire, sendRelease };
}

const mkroot = (tag) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), `scpc-mp-${tag}-`));
  return dir.replace(/\\/g, '/');
};

(async () => {
  // --- Gate 1: multi-process contention on one root ---
  {
    const root = mkroot('contend');
    const s1 = startSidecar();
    s1.sendAcquire(root);
    const a1 = await s1.waitFor((x) => x.operation === 'acquire');
    check('p1_acquires', a1.success === true, JSON.stringify(a1));

    const s2 = startSidecar();
    const t0 = Date.now();
    s2.sendAcquire(root);
    const a2 = await s2.waitFor((x) => x.operation === 'acquire');
    const waitedMs = Date.now() - t0;
    check('p2_busy_while_p1_holds', a2.success === false && a2.status === 'busy', JSON.stringify(a2));
    check('p2_busy_after_bounded_wait', waitedMs >= 4000, `waited ${waitedMs}ms`);

    s1.sendRelease(a1.lease_id, a1.token);
    await s1.waitFor((x) => x.operation === 'release' && x.success === true);

    const s3 = startSidecar();
    s3.sendAcquire(root);
    const a3 = await s3.waitFor((x) => x.operation === 'acquire');
    check('p3_acquires_after_release', a3.success === true, JSON.stringify(a3));
    s3.sendRelease(a3.lease_id, a3.token);
    await s3.waitFor((x) => x.operation === 'release' && x.success === true);
    check('lock_removed_after_release', !fs.existsSync(path.join(root, 'publish.lock')));
    s1.proc.stdin.end(); s2.proc.stdin.end(); s3.proc.stdin.end();
    await Promise.all([s1, s2, s3].map((s) => new Promise((r) => s.proc.on('exit', r))));
  }

  // --- Gate 2: crash leaves abandoned mutex + stale owner; next process recovers ---
  {
    const root = mkroot('crash');
    const s1 = startSidecar();
    s1.sendAcquire(root);
    const a1 = await s1.waitFor((x) => x.operation === 'acquire');
    check('crash_p1_acquired', a1.success === true);

    s1.proc.kill();
    await new Promise((r) => s1.proc.on('exit', r));
    check('stale_owner_left_by_crash', fs.existsSync(path.join(root, 'publish.lock')));

    const s2 = startSidecar();
    s2.sendAcquire(root);
    const a2 = await s2.waitFor((x) => x.operation === 'acquire', 20000);
    check('p2_recovers_after_crash', a2.success === true, JSON.stringify(a2));
    const lock = fs.readFileSync(path.join(root, 'publish.lock'), 'utf8');
    check('owner_replaced_by_recoverer', lock.includes(a2.token));

    s2.sendRelease(a2.lease_id, a2.token);
    await s2.waitFor((x) => x.operation === 'release' && x.success === true);
    s2.proc.stdin.end();
    await new Promise((r) => s2.proc.on('exit', r));
  }

  // --- Gate 3: cross-root isolation ---
  {
    const rootA = mkroot('isoA');
    const rootB = mkroot('isoB');
    const sA = startSidecar();
    sA.sendAcquire(rootA);
    const aA = await sA.waitFor((x) => x.operation === 'acquire');
    check('isoA_acquired', aA.success === true);

    const sB = startSidecar();
    const t0 = Date.now();
    sB.sendAcquire(rootB);
    const aB = await sB.waitFor((x) => x.operation === 'acquire');
    check('different_root_not_blocked', aB.success === true, JSON.stringify(aB));
    check('different_root_fast', Date.now() - t0 < 2000, 'took too long');

    sA.sendRelease(aA.lease_id, aA.token);
    sB.sendRelease(aB.lease_id, aB.token);
    await Promise.all([
      sA.waitFor((x) => x.operation === 'release' && x.success === true),
      sB.waitFor((x) => x.operation === 'release' && x.success === true),
    ]);
    sA.proc.stdin.end(); sB.proc.stdin.end();
    await Promise.all([sA, sB].map((s) => new Promise((r) => s.proc.on('exit', r))));
  }

  console.log(failures.length === 0 ? 'ALL MULTIPROC GATES PASSED' : `GATE FAILURES: ${failures.join(', ')}`);
  process.exit(failures.length === 0 ? 0 : 1);
})().catch((e) => {
  console.error('driver error:', e);
  process.exit(1);
});
