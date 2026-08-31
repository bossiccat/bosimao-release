'use strict';

const crypto = require('crypto');

const ACK_REPORTERS = Object.freeze({
  android_trtc_left: Object.freeze(['android']),
  sidecar_trtc_left: Object.freeze(['sidecar']),
  bridge_drained_closed: Object.freeze(['sidecar', 'rtc_bridge']),
  apm_cancelled_closed: Object.freeze(['rtc_bridge']),
  brain_turns_sealed: Object.freeze(['brain']),
});
const ACK_NAMES = Object.freeze(Object.keys(ACK_REPORTERS));
const TERMINATE_REASONS = new Set([
  'user_stop', 'remote_leave', 'app_shutdown', 'security_revoke', 'error_recovery',
]);
const RETRY_REASON = Object.freeze({
  partial: 'retry_failed_acknowledgements',
  timeout: 'retry_timeout',
});

class ContractError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

function initialAcks() {
  return Object.fromEntries(ACK_NAMES.map((name) => [name, 'pending']));
}

function assertObject(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new ContractError(40001, 'invalid request body');
  }
}

function assertExactKeys(value, required, optional = []) {
  assertObject(value);
  const allowed = new Set([...required, ...optional]);
  if (required.some((key) => !Object.hasOwn(value, key))) {
    throw new ContractError(40001, 'missing required field');
  }
  if (Object.keys(value).some((key) => !allowed.has(key))) {
    throw new ContractError(40001, 'unexpected request field');
  }
}

function assertText(value, field, max = 512) {
  if (typeof value !== 'string' || !value.trim() || value.length > max) {
    throw new ContractError(40001, `${field} is invalid`);
  }
}

function assertGeneration(value) {
  if (!Number.isInteger(value) || value < 0) {
    throw new ContractError(40001, 'generation is invalid');
  }
}

function validateTerminate(body, pathSessionId) {
  const fields = [
    'session_id', 'device_id', 'room_id', 'generation', 'request_id', 'reason', 'requested_at',
  ];
  assertExactKeys(body, fields);
  for (const field of ['session_id', 'device_id', 'room_id', 'request_id', 'requested_at']) {
    assertText(body[field], field);
  }
  assertGeneration(body.generation);
  if (!TERMINATE_REASONS.has(body.reason)) throw new ContractError(40001, 'reason is invalid');
  if (body.session_id !== pathSessionId) throw new ContractError(40916, 'session context mismatch');
  return body;
}

function validateRetry(body) {
  assertExactKeys(body, ['request_id', 'reason']);
  assertText(body.request_id, 'request_id');
  if (!Object.values(RETRY_REASON).includes(body.reason)) {
    throw new ContractError(40001, 'reason is invalid');
  }
  return body;
}

function validateAck(body, pathSessionId) {
  const required = [
    'acknowledgement', 'result', 'session_id', 'device_id', 'room_id', 'generation', 'reported_at',
  ];
  assertExactKeys(body, required, ['error_code']);
  for (const field of ['session_id', 'device_id', 'room_id', 'reported_at']) {
    assertText(body[field], field);
  }
  assertGeneration(body.generation);
  if (!ACK_REPORTERS[body.acknowledgement]) throw new ContractError(40001, 'acknowledgement is invalid');
  if (!['confirmed', 'failed'].includes(body.result)) throw new ContractError(40001, 'result is invalid');
  if (body.result === 'failed') assertText(body.error_code, 'error_code', 128);
  if (body.result === 'confirmed' && body.error_code !== undefined) {
    throw new ContractError(40001, 'error_code must be omitted');
  }
  if (body.session_id !== pathSessionId) throw new ContractError(40403, 'termination not found');
  return body;
}

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

function payloadHash(value) {
  return crypto.createHash('sha256').update(canonical(value), 'utf8').digest('hex');
}

function requestKey(sessionId, generation, requestId) {
  return crypto.createHash('sha256')
    .update(`${sessionId}\0${generation}\0${requestId}`, 'utf8').digest('hex');
}

function statusData(record) {
  const scope = record.operation === 'terminate' ? 'root' : 'retry_child';
  const result = record.result;
  const data = {
    type: result === 'pending' ? 'session.terminating' : 'session.terminated',
    scope,
    status_variant: `${scope}_${result}`,
    termination_id: record._id,
    session_id: record.session_id,
    generation: record.generation,
    result,
    acknowledgements: record.acknowledgements,
    terminal_at: record.terminal_at ? new Date(record.terminal_at).toISOString() : null,
    retryable: result === 'partial' || result === 'timeout',
  };
  if (scope === 'retry_child') data.parent_termination_id = record.parent_termination_id;
  return data;
}

module.exports = {
  ACK_NAMES, ACK_REPORTERS, RETRY_REASON, ContractError, initialAcks,
  payloadHash, requestKey, statusData, validateAck, validateRetry, validateTerminate,
};
