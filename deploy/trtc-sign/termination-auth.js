'use strict';

const crypto = require('crypto');

const devices = require('./devices');

const STATIC_REPORTERS = Object.freeze([
  ['VOICE_SIDECAR_CREDENTIAL', 'sidecar'],
  ['VOICE_RTC_BRIDGE_CREDENTIAL', 'rtc_bridge'],
  ['VOICE_BRAIN_SERVICE_CREDENTIAL', 'brain'],
]);

function tokenOf(event) {
  const headers = event.headers || {};
  const authorization = headers.authorization || headers.Authorization || '';
  const match = String(authorization).match(/^Bearer\s+(\S+)$/);
  return match ? match[1] : '';
}

function nonceOf(event) {
  const headers = event.headers || {};
  return String(headers['x-request-nonce'] || headers['X-Request-Nonce'] || '');
}

function equalSecret(left, right) {
  if (!left || !right) return false;
  const a = Buffer.from(left, 'utf8');
  const b = Buffer.from(right, 'utf8');
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

function staticPrincipal(token) {
  for (const [envName, reporter] of STATIC_REPORTERS) {
    if (equalSecret(token, process.env[envName] || '')) {
      return { type: reporter, reporter, subjectId: reporter };
    }
  }
  return null;
}

async function devicePrincipal(token) {
  const match = token.match(/^([^\.\s]+)\.(\S+)$/);
  if (!match) return null;
  const valid = await devices.verifyDeviceCredential(match[1], match[2]);
  return valid ? { type: 'device', reporter: 'android', subjectId: match[1] } : null;
}

async function authenticateDevice(event) {
  return await devicePrincipal(tokenOf(event));
}

async function authenticateStatus(event) {
  const token = tokenOf(event);
  return await devicePrincipal(token) || (() => {
    const principal = staticPrincipal(token);
    return principal && principal.type === 'sidecar' ? principal : null;
  })();
}

async function authenticateAck(event) {
  const token = tokenOf(event);
  return await devicePrincipal(token) || staticPrincipal(token);
}

function requireNonce(event) {
  const nonce = nonceOf(event);
  return nonce.length >= 16 && nonce.length <= 128 ? nonce : null;
}

module.exports = {
  authenticateAck, authenticateDevice, authenticateStatus, requireNonce,
};
