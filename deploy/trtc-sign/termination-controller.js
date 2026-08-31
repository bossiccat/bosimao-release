'use strict';

const auth = require('./termination-auth');
const contract = require('./termination-contract');
const service = require('./termination-service');
const store = require('./termination-store');

const ERROR_MESSAGE = Object.freeze({
  40001: 'bad_request', 40101: 'unauthorized', 40102: 'nonce_replay',
  40402: 'session_not_found', 40403: 'termination_not_found',
  40901: 'state_conflict', 40912: 'idempotency_key_payload_mismatch',
  40913: 'termination_not_retryable', 40916: 'request_id_reused',
  50301: 'termination_unconfirmed',
});

function success(data, statusCode = 200) {
  return { code: 0, data, message: '', statusCode };
}

function failure(code) {
  return { code, data: null, message: ERROR_MESSAGE[code] || 'error' };
}

async function withNonce(event, principal) {
  const nonce = auth.requireNonce(event);
  if (!nonce || !await store.consumeNonce(principal.subjectId, nonce)) return failure(40102);
  return null;
}

function accepted(record) {
  return success({
    termination_id: record._id,
    ...(record.parent_termination_id ? { parent_termination_id: record.parent_termination_id } : {}),
    session_id: record.session_id,
    generation: record.generation,
    state: 'TERMINATING',
    status_url: `/api/v1/voice/sessions/${record.session_id}/termination/${record._id}`,
  }, 202);
}

async function terminate(event, sessionId, body) {
  const principal = await auth.authenticateDevice(event);
  if (!principal) return failure(40101);
  const nonceError = await withNonce(event, principal);
  if (nonceError) return nonceError;
  if (body.device_id !== principal.subjectId) return failure(40916);
  return accepted(await service.terminate(sessionId, body));
}

async function status(event, sessionId, terminationId) {
  const principal = await auth.authenticateStatus(event);
  if (!principal) return failure(40101);
  const data = await service.status(sessionId, terminationId);
  if (principal.type === 'device') {
    const record = await store.getTermination(terminationId);
    if (record.device_id !== principal.subjectId) return failure(40101);
  }
  return success(data);
}

async function retry(event, sessionId, terminationId, body) {
  const principal = await auth.authenticateDevice(event);
  if (!principal) return failure(40101);
  const nonceError = await withNonce(event, principal);
  if (nonceError) return nonceError;
  const parent = await store.getTermination(terminationId);
  if (parent.device_id !== principal.subjectId) return failure(40101);
  return accepted(await service.retry(sessionId, terminationId, body));
}

async function acknowledge(event, sessionId, terminationId, body) {
  const principal = await auth.authenticateAck(event);
  if (!principal) return failure(40101);
  const nonceError = await withNonce(event, principal);
  if (nonceError) return nonceError;
  if (principal.type === 'device') {
    const termination = await store.getTermination(terminationId);
    if (termination.device_id !== principal.subjectId) return failure(40101);
  }
  const record = await service.acknowledge(sessionId, terminationId, principal.reporter, body);
  return success({
    termination_id: record._id,
    session_id: record.session_id,
    generation: record.generation,
    acknowledgement: body.acknowledgement,
    ack_result: record.acknowledgements[body.acknowledgement],
    result: record.result,
    status_url: `/api/v1/voice/sessions/${record.session_id}/termination/${record._id}`,
  }, 202);
}

async function dispatch(handler) {
  try {
    return await handler();
  } catch (error) {
    if (error instanceof contract.ContractError || Number.isInteger(error && error.code)) {
      return failure(error.code);
    }
    throw error;
  }
}

module.exports = {
  acknowledge: (...args) => dispatch(() => acknowledge(...args)),
  retry: (...args) => dispatch(() => retry(...args)),
  status: (...args) => dispatch(() => status(...args)),
  terminate: (...args) => dispatch(() => terminate(...args)),
};
