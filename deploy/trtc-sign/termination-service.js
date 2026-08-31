'use strict';

const contract = require('./termination-contract');
const store = require('./termination-store');

async function registerSession(session) {
  return await store.createSession(session);
}

async function terminate(pathSessionId, body) {
  const payload = contract.validateTerminate(body, pathSessionId);
  return await store.beginTermination({
    sessionId: pathSessionId,
    generation: payload.generation,
    requestId: payload.request_id,
    hash: contract.payloadHash(payload),
    payload,
  });
}

async function status(pathSessionId, terminationId) {
  const record = await store.getTermination(terminationId);
  if (record.session_id !== pathSessionId) throw new contract.ContractError(40403, 'termination not found');
  return contract.statusData(record);
}

async function retry(pathSessionId, parentId, body) {
  const payload = contract.validateRetry(body);
  const hashPayload = {
    session_id: pathSessionId,
    parent_termination_id: parentId,
    reason: payload.reason,
  };
  return await store.retryTermination({
    sessionId: pathSessionId,
    parentId,
    requestId: payload.request_id,
    reason: payload.reason,
    hash: contract.payloadHash(hashPayload),
  });
}

async function acknowledge(pathSessionId, terminationId, reporter, body) {
  const payload = contract.validateAck(body, pathSessionId);
  return await store.recordAck({
    terminationId,
    context: payload,
    acknowledgement: payload.acknowledgement,
    reporter,
    result: payload.result,
    errorCode: payload.error_code,
  });
}

module.exports = { acknowledge, registerSession, retry, status, terminate };
