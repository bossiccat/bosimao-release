'use strict';
// End-to-end smoke test for the sidecar-publish-coordination binary.
// Drives the real process over stdin/stdout with the newline JSON protocol
// and verifies on-disk state transitions. NOT a static check.

const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const EXE = 'C:/Users/Administrator/WorkBuddy/监视app/tools/sidecar-publish-coordination/target/debug/sidecar-publish-coordination.exe';
const ROOT = fs.mkdtempSync(path.join(os.tmpdir(), 'scpc-e2e-'));
const TOKEN = '550e8400-e29b-41d4-a716-446655440000';
const TOKEN2 = '660e8400-e29b-41d4-a716-446655440001';

function uuid() {
  return crypto.randomUUID();
}
const crypto = require('node:crypto');

function send(proc, obj) {
  proc.stdin.write(JSON.stringify(obj) + '\n');
}

const failures = [];
function check(name, condition, detail) {
  if (condition) {
    console.log(`PASS ${name}`);
  } else {
    failures.push(name);
    console.log(`FAIL ${name} ${detail || ''}`);
  }
}

const proc = spawn(EXE, [], { stdio: ['pipe', 'pipe', 'pipe'] });
let buffer = '';
const responses = [];
proc.stdout.on('data', (chunk) => {
  buffer += chunk.toString('utf8');
  let index;
  while ((index = buffer.indexOf('\n')) >= 0) {
    const line = buffer.slice(0, index).trim();
    buffer = buffer.slice(index + 1);
    if (line) responses.push(JSON.parse(line));
  }
});
proc.stderr.on('data', (c) => process.stderr.write(c));

function waitFor(predicate, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  const start = responses.length;
  return new Promise((resolve, reject) => {
    (function poll() {
      const hit = responses.slice(start).find(predicate);
      if (hit) return resolve(hit);
      if (Date.now() > deadline) return reject(new Error('timeout waiting for response'));
      setTimeout(poll, 25);
    })();
  });
}

(async () => {
  // 1. invalid request is rejected
  send(proc, { operation: 'bogus' });
  let r = await waitFor((x) => x.operation === 'protocol' && x.status === 'invalid_request');
  check('invalid_request_rejected', true);

  // 2. acquire with a live self-owner (current process identity)
  const now = new Date().toISOString().replace(/\.\d{3}Z$/, '.000Z');
  send(proc, {
    operation: 'acquire',
    runtime_root: ROOT.replace(/\\/g, '/'),
    owner: {
      schema_version: 1,
      token: TOKEN,
      pid: process.pid,
      created_at: now,
      process_creation_time: '2026-08-19T00:00:00.000Z', // placeholder identity fields; probe decides live/dead
      process_creation_identity: 'placeholder',
    },
    timeout_ms: 5000,
  });
  r = await waitFor((x) => x.operation === 'acquire');
  // Self PID is live but identity mismatch -> PidReused -> reclaimed -> acquired.
  check('acquire_succeeds_over_pid_reused_owner', r.success === true && r.status === 'acquired', JSON.stringify(r));

  // publish.lock on disk contains our token
  const lock = fs.readFileSync(path.join(ROOT, 'publish.lock'), 'utf8');
  check('lock_file_contains_token', lock.includes(TOKEN));

  // 3. publish under the lease
  fs.writeFileSync(path.join(ROOT, 'next.tmp'), '{"generation":"g-e2e"}');
  send(proc, {
    operation: 'publish',
    lease_id: r.lease_id,
    temporary_path: path.join(ROOT, 'next.tmp'),
    current_path: path.join(ROOT, 'current.json'),
  });
  const pub = await waitFor((x) => x.operation === 'publish');
  check('publish_commits_pointer', pub.success === true && pub.status === 'committed', JSON.stringify(pub));
  check(
    'pointer_on_disk',
    fs.readFileSync(path.join(ROOT, 'current.json'), 'utf8') === '{"generation":"g-e2e"}'
  );
  check('first_publish_renamed_tmp', !fs.existsSync(path.join(ROOT, 'next.tmp')));

  // 4. second publish to test ReplaceFileW path (current.json now exists)
  fs.writeFileSync(path.join(ROOT, 'next2.tmp'), '{"generation":"g-e2e-2"}');
  send(proc, {
    operation: 'publish',
    lease_id: r.lease_id,
    temporary_path: path.join(ROOT, 'next2.tmp'),
    current_path: path.join(ROOT, 'current.json'),
  });
  const pub2 = await waitFor(
    (x) => x.operation === 'publish' && x.diagnostic && x.diagnostic.includes('atomically')
  );
  check(
    'second_publish_uses_replace',
    fs.readFileSync(path.join(ROOT, 'current.json'), 'utf8') === '{"generation":"g-e2e-2"}'
  );
  check('replace_leaves_backup', fs.existsSync(path.join(ROOT, 'current.json.bak')));

  // 5. release with wrong token -> owner mismatch
  send(proc, { operation: 'release', lease_id: r.lease_id, expected_token: TOKEN2 });
  const relBad = await waitFor(
    (x) => x.operation === 'release' && x.status === 'owner_mismatch'
  );
  check('release_wrong_token_fails', relBad.success === false, JSON.stringify(relBad));
  check('lock_survives_wrong_token', fs.existsSync(path.join(ROOT, 'publish.lock')));

  // 6. release with correct token
  send(proc, { operation: 'release', lease_id: r.lease_id, expected_token: TOKEN });
  const relOk = await waitFor(
    (x) => x.operation === 'release' && x.success === true
  );
  check('release_removes_lock', !fs.existsSync(path.join(ROOT, 'publish.lock')));

  // 7. publish after release -> no_active_lease
  send(proc, {
    operation: 'publish',
    lease_id: 'x',
    temporary_path: 'a',
    current_path: 'b',
  });
  const pubNoLease = await waitFor(
    (x) => x.operation === 'publish' && x.status === 'no_active_lease'
  );
  check('publish_without_lease_rejected', pubNoLease.success === false);

  proc.stdin.end();
  await new Promise((resolve) => proc.on('exit', resolve));

  fs.rmSync(ROOT, { recursive: true, force: true });
  console.log(failures.length === 0 ? 'ALL E2E CHECKS PASSED' : `E2E FAILURES: ${failures.join(', ')}`);
  process.exit(failures.length === 0 ? 0 : 1);
})().catch((error) => {
  console.error('E2E driver error:', error);
  process.exit(1);
});
