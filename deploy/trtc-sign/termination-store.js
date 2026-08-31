'use strict';

const crypto = require('crypto');

const COLL_SESSION = 'voice_control_sessions';
const COLL_TERMINATION = 'voice_terminations';
const COLL_REQUEST = 'voice_termination_requests';
const COLL_NONCE = 'voice_consumed_nonces';

let _db = null;
function db() {
  if (_db) return _db;
  const tcb = require('@cloudbase/node-sdk');
  const app = tcb.init({
    env: tcb.SYMBOL_DEFAULT_ENV,
    ...(process.env.CLOUDBASE_APIKEY ? { accessKey: process.env.CLOUDBASE_APIKEY } : {}),
  });
  _db = app.database();
  return _db;
}

function unwrap(result) {
  const data = result && result.data;
  if (Array.isArray(data)) return data[0] || null;
  return data || null;
}

async function getDoc(database, collection, id) {
  return unwrap(await database.collection(collection).doc(id).get());
}

async function setDoc(database, collection, id, value) {
  await database.collection(collection).doc(id).set(value);
  return value;
}

function nonceId(subjectId, nonce) {
  return crypto.createHash('sha256').update(`${subjectId}\0${nonce}`, 'utf8').digest('hex');
}

async function consumeNonce(subjectId, nonce) {
  const id = nonceId(subjectId, nonce);
  const now = Date.now();
  return await db().runTransaction(async (transaction) => {
    const existing = await getDoc(transaction, COLL_NONCE, id);
    if (existing && existing.expires_at > now) return false;
    await setDoc(transaction, COLL_NONCE, id, {
      subject_id: subjectId,
      nonce_hash: crypto.createHash('sha256').update(nonce, 'utf8').digest('hex'),
      expires_at: now + 300000,
      created_at: now,
    });
    return true;
  });
}

async function createSession(session) {
  const now = Date.now();
  return await db().runTransaction(async (transaction) => {
    const current = await getDoc(transaction, COLL_SESSION, session.session_id);
    if (current) {
      const same = current.device_id === session.device_id && current.room_id === session.room_id &&
        current.generation === session.generation;
      if (!same) throw Object.assign(new Error('session context mismatch'), { code: 40916 });
      return current;
    }
    const record = { ...session, state: 'ACTIVE', created_at: now, updated_at: now };
    await setDoc(transaction, COLL_SESSION, session.session_id, record);
    return { ...record, _id: session.session_id };
  });
}

async function beginTermination({ sessionId, generation, requestId, hash, payload }) {
  const requestDocId = require('./termination-contract').requestKey(sessionId, generation, requestId);
  const now = Date.now();
  return await db().runTransaction(async (transaction) => {
    const session = await getDoc(transaction, COLL_SESSION, sessionId);
    if (!session) throw Object.assign(new Error('session not found'), { code: 40402 });
    const expected = [session._id || sessionId, session.device_id, session.room_id, session.generation];
    const actual = [payload.session_id, payload.device_id, payload.room_id, generation];
    if (expected.some((value, index) => value !== actual[index])) {
      throw Object.assign(new Error('session context mismatch'), { code: 40916 });
    }
    const existing = await getDoc(transaction, COLL_REQUEST, requestDocId);
    if (existing) {
      if (existing.payload_hash !== hash) throw Object.assign(new Error('idempotency mismatch'), { code: 40912 });
      return await getDoc(transaction, COLL_TERMINATION, existing.termination_id);
    }
    if (session.state !== 'ACTIVE') throw Object.assign(new Error('request id reused'), { code: 40916 });
    const terminationId = crypto.randomUUID();
    const record = {
      session_id: sessionId, device_id: session.device_id, room_id: session.room_id,
      generation, operation: 'terminate', request_id: requestId, payload_hash: hash,
      parent_termination_id: null, result: 'pending', state: 'TERMINATING',
      acknowledgements: require('./termination-contract').initialAcks(),
      ack_reports: {}, terminal_at: null, created_at: now, updated_at: now,
    };
    await setDoc(transaction, COLL_TERMINATION, terminationId, record);
    await setDoc(transaction, COLL_REQUEST, requestDocId, {
      termination_id: terminationId, payload_hash: hash, operation: 'terminate', created_at: now,
    });
    await transaction.collection(COLL_SESSION).doc(sessionId).update({ state: 'TERMINATING', updated_at: now });
    return { ...record, _id: terminationId };
  });
}

async function getTermination(terminationId) {
  const record = await getDoc(db(), COLL_TERMINATION, terminationId);
  if (!record) throw Object.assign(new Error('termination not found'), { code: 40403 });
  return record;
}

async function retryTermination({ sessionId, parentId, requestId, reason, hash }) {
  const now = Date.now();
  return await db().runTransaction(async (transaction) => {
    const parent = await getDoc(transaction, COLL_TERMINATION, parentId);
    if (!parent || parent.session_id !== sessionId) throw Object.assign(new Error('termination not found'), { code: 40403 });
    const requestDocId = require('./termination-contract').requestKey(sessionId, parent.generation, requestId);
    const existing = await getDoc(transaction, COLL_REQUEST, requestDocId);
    if (existing) {
      if (existing.payload_hash !== hash) throw Object.assign(new Error('idempotency mismatch'), { code: 40912 });
      return await getDoc(transaction, COLL_TERMINATION, existing.termination_id);
    }
    const session = await getDoc(transaction, COLL_SESSION, sessionId);
    const expectedReason = require('./termination-contract').RETRY_REASON[parent.result];
    const expectedState = parent.result === 'partial' ? 'TERMINATION_PARTIAL' : 'TERMINATION_TIMEOUT';
    if (!expectedReason || reason !== expectedReason || !session || session.state !== expectedState ||
        parent.child_termination_id) {
      throw Object.assign(new Error('termination not retryable'), { code: 40913 });
    }
    const childId = crypto.randomUUID();
    const inherited = {};
    for (const [name, result] of Object.entries(parent.acknowledgements || {})) {
      if (result === 'confirmed') inherited[name] = 'confirmed';
    }
    const record = {
      session_id: sessionId, device_id: parent.device_id, room_id: parent.room_id,
      generation: parent.generation, operation: 'retry', request_id: requestId,
      payload_hash: hash, parent_termination_id: parentId, result: 'pending',
      state: 'TERMINATING', acknowledgements: {
        ...require('./termination-contract').initialAcks(), ...inherited,
      }, inherited_acknowledgements: Object.keys(inherited), ack_reports: {},
      terminal_at: null, created_at: now, updated_at: now,
    };
    await setDoc(transaction, COLL_TERMINATION, childId, record);
    await setDoc(transaction, COLL_REQUEST, requestDocId, {
      termination_id: childId, payload_hash: hash, operation: 'retry', created_at: now,
    });
    await transaction.collection(COLL_TERMINATION).doc(parentId)
      .update({ child_termination_id: childId, updated_at: now });
    await transaction.collection(COLL_SESSION).doc(sessionId).update({ state: 'TERMINATING', updated_at: now });
    return { ...record, _id: childId };
  });
}

async function recordAck({ terminationId, context, acknowledgement, reporter, result, errorCode }) {
  const contract = require('./termination-contract');
  const now = Date.now();
  return await db().runTransaction(async (transaction) => {
    const record = await getDoc(transaction, COLL_TERMINATION, terminationId);
    if (!record) throw Object.assign(new Error('termination not found'), { code: 40403 });
    const expected = [record.session_id, record.device_id, record.room_id, record.generation];
    const actual = [context.session_id, context.device_id, context.room_id, context.generation];
    if (expected.some((value, index) => value !== actual[index])) {
      throw Object.assign(new Error('ack context mismatch'), { code: 40901 });
    }
    if (record.result !== 'pending' || (record.inherited_acknowledgements || []).includes(acknowledgement)) {
      throw Object.assign(new Error('termination is terminal'), { code: 40901 });
    }
    if (!contract.ACK_REPORTERS[acknowledgement].includes(reporter)) {
      throw Object.assign(new Error('reporter unauthorized'), { code: 40901 });
    }
    const reports = { ...(record.ack_reports || {}) };
    reports[acknowledgement] = { ...(reports[acknowledgement] || {}) };
    reports[acknowledgement][reporter] = { result, error_code: errorCode || null, reported_at: now };
    const required = contract.ACK_REPORTERS[acknowledgement];
    const latest = reports[acknowledgement];
    const aggregate = Object.values(latest).some((report) => report.result === 'failed') ? 'failed'
      : required.every((name) => latest[name] && latest[name].result === 'confirmed') ? 'confirmed'
        : 'pending';
    const acknowledgements = { ...record.acknowledgements, [acknowledgement]: aggregate };
    const complete = contract.ACK_NAMES.every((name) => acknowledgements[name] === 'confirmed');
    const patch = {
      ack_reports: reports, acknowledgements, updated_at: now,
      ...(complete ? { result: 'complete', state: 'TERMINATED', terminal_at: now } : {}),
    };
    await transaction.collection(COLL_TERMINATION).doc(terminationId).update(patch);
    if (complete) {
      await transaction.collection(COLL_SESSION).doc(record.session_id)
        .update({ state: 'TERMINATED', updated_at: now });
    }
    return { ...record, ...patch };
  });
}

module.exports = {
  beginTermination, consumeNonce, createSession, getTermination, recordAck, retryTermination,
  _setDbForTest: (database) => { _db = database; },
  _collections: { COLL_SESSION, COLL_TERMINATION, COLL_REQUEST, COLL_NONCE },
};
