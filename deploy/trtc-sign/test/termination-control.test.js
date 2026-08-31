'use strict';

process.env.TRTC_SDKAPPID = '1600155678';
process.env.TRTC_SECRETKEY = 'fake-secret-key-for-test-only-0123456789';
process.env.VOICE_SIDECAR_CREDENTIAL = 'sidecar-test-secret';
process.env.VOICE_OWNER_CREDENTIAL = 'owner-test-secret';
process.env.VOICE_RTC_BRIDGE_CREDENTIAL = 'rtc-bridge-test-secret';
process.env.VOICE_BRAIN_SERVICE_CREDENTIAL = 'brain-test-secret';

const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('crypto');

const devices = require('../devices');
const signing = require('../signing');
const terminationStore = require('../termination-store');
const index = require('../index');

function clone(value) {
  return value === undefined ? undefined : structuredClone(value);
}

function makeDb() {
  const collections = new Map();
  const command = {
    inc: (value) => ({ __op: 'inc', value }),
    lt: (value) => ({ __op: 'lt', value }),
  };

  function storeFor(name) {
    if (!collections.has(name)) collections.set(name, new Map());
    return collections.get(name);
  }

  function matches(id, doc, query) {
    return Object.entries(query).every(([key, value]) => {
      const actual = key === '_id' ? id : doc[key];
      if (value && value.__op === 'lt') return actual < value.value;
      return actual === value;
    });
  }

  function applyPatch(doc, patch) {
    for (const [key, value] of Object.entries(patch)) {
      if (value && value.__op === 'inc') doc[key] = Number(doc[key] || 0) + value.value;
      else doc[key] = clone(value);
    }
  }

  function collection(name) {
    const store = storeFor(name);
    return {
      doc(id) {
        return {
          async get() {
            return { data: store.has(id) ? clone(store.get(id)) : null };
          },
          async set(data) {
            store.set(id, { ...clone(data), _id: id });
            return { updated: 1 };
          },
          async update(patch) {
            if (!store.has(id)) return { updated: 0 };
            applyPatch(store.get(id), patch);
            return { updated: 1 };
          },
          async remove() {
            const deleted = store.delete(id);
            return { deleted: deleted ? 1 : 0 };
          },
        };
      },
      where(query) {
        return {
          async get() {
            return { data: [...store].filter(([id, doc]) => matches(id, doc, query)).map(([, doc]) => clone(doc)) };
          },
          limit() { return this; },
          async update(patch) {
            let updated = 0;
            for (const [id, doc] of store) {
              if (!matches(id, doc, query)) continue;
              applyPatch(doc, patch);
              updated += 1;
            }
            return { updated };
          },
        };
      },
    };
  }

  return {
    command,
    collection,
    async runTransaction(callback) {
      const snapshot = clone([...collections].map(([name, store]) => [name, [...store]]));
      try {
        return await callback({ collection });
      } catch (error) {
        collections.clear();
        for (const [name, entries] of snapshot) collections.set(name, new Map(entries));
        throw error;
      }
    },
    _get(name, id) { return clone(storeFor(name).get(id)); },
    _set(name, id, value) { storeFor(name).set(id, { ...clone(value), _id: id }); },
  };
}

function secretHash(secret) {
  return crypto.createHash('sha256').update(secret, 'utf8').digest('hex');
}

function bearer(token, nonce) {
  const headers = { Authorization: `Bearer ${token}` };
  if (nonce) headers['X-Request-Nonce'] = nonce;
  return headers;
}

function nonce(label) {
  return `${label}-0123456789abcdef`;
}

async function call(method, path, body, headers = {}) {
  const response = await index.main_handler({
    httpMethod: method,
    path,
    headers,
    body: body === undefined ? '' : JSON.stringify(body),
  }, {});
  return { response, payload: JSON.parse(response.body) };
}

let db;
const DEVICE_ID = 'device-1';
const DEVICE_SECRET = 'device-test-secret';
const DEVICE_TOKEN = `${DEVICE_ID}.${DEVICE_SECRET}`;

async function createSession(sessionId = 'session-1') {
  const result = await call('POST', '/api/v1/voice/session', {
    device_id: DEVICE_ID,
    session_id: sessionId,
    generation: 0,
  }, bearer(DEVICE_TOKEN));
  assert.equal(result.response.statusCode, 200, JSON.stringify(result.payload));
  return {
    session_id: sessionId,
    device_id: DEVICE_ID,
    room_id: `jax-${DEVICE_ID}`,
    generation: 0,
  };
}

function terminateBody(session, requestId = 'request-1') {
  return {
    ...session,
    request_id: requestId,
    reason: 'user_stop',
    requested_at: '2026-08-25T00:00:00Z',
  };
}

function ackBody(session, acknowledgement) {
  return {
    acknowledgement,
    result: 'confirmed',
    ...session,
    reported_at: '2026-08-25T00:00:00Z',
  };
}

test.beforeEach(() => {
  db = makeDb();
  db._set('voice_devices', DEVICE_ID, {
    credential_secret_hash: secretHash(DEVICE_SECRET),
    expires_at: Math.floor(Date.now() / 1000) + 3600,
    revoked: false,
  });
  devices._setDbForTest(db);
  signing._setDbForTest(db);
  terminationStore._setDbForTest(db);
});

test('terminate is idempotent for the same request and rejects changed payload', async () => {
  const session = await createSession();
  const body = terminateBody(session);
  const first = await call('POST', `/api/v1/voice/sessions/${session.session_id}/terminate`, body,
    bearer(DEVICE_TOKEN, nonce('terminate-first')));
  const replay = await call('POST', `/api/v1/voice/sessions/${session.session_id}/terminate`, body,
    bearer(DEVICE_TOKEN, nonce('terminate-replay')));
  assert.equal(first.response.statusCode, 202);
  assert.equal(replay.response.statusCode, 202);
  assert.equal(replay.payload.data.termination_id, first.payload.data.termination_id);

  const changed = await call('POST', `/api/v1/voice/sessions/${session.session_id}/terminate`,
    { ...body, reason: 'app_shutdown' }, bearer(DEVICE_TOKEN, nonce('terminate-changed')));
  assert.equal(changed.response.statusCode, 409);
  assert.equal(changed.payload.code, 40912);
});

test('terminate consumes nonce atomically and rejects replay', async () => {
  const session = await createSession();
  const headers = bearer(DEVICE_TOKEN, nonce('shared-nonce'));
  const first = await call('POST', `/api/v1/voice/sessions/${session.session_id}/terminate`,
    terminateBody(session), headers);
  const replay = await call('POST', `/api/v1/voice/sessions/${session.session_id}/terminate`,
    terminateBody(session), headers);
  assert.equal(first.response.statusCode, 202);
  assert.equal(replay.response.statusCode, 401);
  assert.equal(replay.payload.code, 40102);
});

test('termination status allows own device or sidecar without nonce and rejects owner', async () => {
  const session = await createSession();
  const accepted = await call('POST', `/api/v1/voice/sessions/${session.session_id}/terminate`,
    terminateBody(session), bearer(DEVICE_TOKEN, nonce('status-create')));
  const tid = accepted.payload.data.termination_id;
  const path = `/api/v1/voice/sessions/${session.session_id}/termination/${tid}`;
  const deviceStatus = await call('GET', path, undefined, bearer(DEVICE_TOKEN));
  const sidecarStatus = await call('GET', path, undefined, bearer('sidecar-test-secret'));
  const ownerStatus = await call('GET', path, undefined, bearer('owner-test-secret'));
  assert.equal(deviceStatus.response.statusCode, 200);
  assert.equal(sidecarStatus.response.statusCode, 200);
  assert.equal(ownerStatus.response.statusCode, 401);
  assert.equal(deviceStatus.payload.data.status_variant, 'root_pending');
});

test('ack derives four reporters, rejects owner, and requires both bridge reports', async () => {
  const session = await createSession();
  const accepted = await call('POST', `/api/v1/voice/sessions/${session.session_id}/terminate`,
    terminateBody(session), bearer(DEVICE_TOKEN, nonce('ack-create')));
  const tid = accepted.payload.data.termination_id;
  const path = `/api/v1/voice/sessions/${session.session_id}/termination/${tid}/acknowledgements`;

  const owner = await call('POST', path, ackBody(session, 'android_trtc_left'),
    bearer('owner-test-secret', nonce('ack-owner')));
  assert.equal(owner.payload.code, 40101);
  const wrongReporter = await call('POST', path, ackBody(session, 'brain_turns_sealed'),
    bearer(DEVICE_TOKEN, nonce('ack-wrong')));
  assert.equal(wrongReporter.payload.code, 40901);

  const reports = [
    ['android_trtc_left', DEVICE_TOKEN, 'ack-android'],
    ['sidecar_trtc_left', 'sidecar-test-secret', 'ack-sidecar-left'],
    ['bridge_drained_closed', 'sidecar-test-secret', 'ack-bridge-sidecar'],
    ['apm_cancelled_closed', 'rtc-bridge-test-secret', 'ack-apm'],
    ['brain_turns_sealed', 'brain-test-secret', 'ack-brain'],
  ];
  for (const [ack, token, nonceLabel] of reports) {
    const result = await call('POST', path, ackBody(session, ack), bearer(token, nonce(nonceLabel)));
    assert.equal(result.response.statusCode, 202, JSON.stringify(result.payload));
  }
  const beforeBridge = await call('GET',
    `/api/v1/voice/sessions/${session.session_id}/termination/${tid}`, undefined,
    bearer('sidecar-test-secret'));
  assert.equal(beforeBridge.payload.data.acknowledgements.bridge_drained_closed, 'pending');
  assert.equal(beforeBridge.payload.data.result, 'pending');

  const finalAck = await call('POST', path, ackBody(session, 'bridge_drained_closed'),
    bearer('rtc-bridge-test-secret', nonce('ack-bridge-rtc')));
  assert.equal(finalAck.response.statusCode, 202);
  assert.equal(finalAck.payload.data.ack_result, 'confirmed');
  assert.equal(finalAck.payload.data.result, 'complete');
});

test('retry requires device nonce, is idempotent, and exposes child status to sidecar', async () => {
  const session = await createSession();
  const accepted = await call('POST', `/api/v1/voice/sessions/${session.session_id}/terminate`,
    terminateBody(session), bearer(DEVICE_TOKEN, nonce('retry-create')));
  const parentId = accepted.payload.data.termination_id;
  const parent = db._get('voice_terminations', parentId);
  db._set('voice_terminations', parentId, {
    ...parent,
    result: 'partial',
    state: 'TERMINATION_PARTIAL',
    terminal_at: Date.now(),
  });
  const sessionDoc = db._get('voice_control_sessions', session.session_id);
  db._set('voice_control_sessions', session.session_id, {
    ...sessionDoc,
    state: 'TERMINATION_PARTIAL',
  });

  const path = `/api/v1/voice/sessions/${session.session_id}/termination/${parentId}/retry`;
  const body = { request_id: 'retry-request-1', reason: 'retry_failed_acknowledgements' };
  const denied = await call('POST', path, body,
    bearer('sidecar-test-secret', nonce('retry-sidecar')));
  assert.equal(denied.payload.code, 40101);
  const first = await call('POST', path, body, bearer(DEVICE_TOKEN, nonce('retry-first')));
  const replay = await call('POST', path, body, bearer(DEVICE_TOKEN, nonce('retry-replay')));
  assert.equal(first.response.statusCode, 202);
  assert.equal(replay.response.statusCode, 202);
  assert.equal(replay.payload.data.termination_id, first.payload.data.termination_id);
  assert.equal(first.payload.data.parent_termination_id, parentId);

  const status = await call('GET', first.payload.data.status_url, undefined,
    bearer('sidecar-test-secret'));
  assert.equal(status.response.statusCode, 200);
  assert.equal(status.payload.data.status_variant, 'retry_child_pending');
});
