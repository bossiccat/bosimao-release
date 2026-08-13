'use strict';

const path = require('path');
const fs = require('fs');

const SIDECAR_USER_ID = 'jax-pc-sidecar';
const VALID_ROLES = new Set(['sidecar', 'phone']);

function loadEnv() {
  const envPath = path.resolve(__dirname, '..', '.env');
  const env = {};
  try {
    const lines = fs.readFileSync(envPath, 'utf8').split(/\r?\n/);
    for (const line of lines) {
      const match = line.match(/^\s*([A-Za-z0-9_]+)\s*=\s*(.*)\s*$/);
      if (match) env[match[1]] = match[2].replace(/^["']|["']$/g, '');
    }
  } catch (_) {
    // Optional non-secret local configuration is absent.
  }
  return env;
}

function parseArgList(args) {
  const out = {
    role: 'sidecar',
    signUrl: 'https://127.0.0.1:8000',
    bridgeUrl: 'ws://127.0.0.1:19092',
    wav: '',
    outWav: '',
    holdS: 120,
    device: undefined,
    invalid: false,
  };
  const seen = new Set();
  const options = new Map([
    ['--device', 'device'], ['--role', 'role'], ['--sign-url', 'signUrl'],
    ['--bridge-url', 'bridgeUrl'], ['--wav', 'wav'], ['--out-wav', 'outWav'],
    ['--hold', 'holdS'],
  ]);
  for (const arg of args) {
    const separator = arg.indexOf('=');
    if (separator < 1) { out.invalid = true; continue; }
    const flag = arg.slice(0, separator);
    const field = options.get(flag);
    if (!field || seen.has(flag)) { out.invalid = true; continue; }
    seen.add(flag);
    const value = arg.slice(separator + 1);
    if (!value) out.invalid = true;
    out[field] = field === 'holdS' ? Number(value) : value;
  }
  if (!Number.isFinite(out.holdS) || out.holdS <= 0) out.invalid = true;
  return out;
}

function parseRendererArgs(search) {
  const query = new URLSearchParams(search || '');
  return parseArgList((query.get('args') || '').split('&').filter(Boolean));
}

function validateStartup(args, runtimeEnv) {
  if (args.invalid || !VALID_ROLES.has(args.role)) return 'SIDECAR_INVALID_ARGS';
  if (args.role === 'sidecar') {
    if (args.device !== undefined) return 'SIDECAR_UNEXPECTED_DEVICE_ARG';
    if (!runtimeEnv.VOICE_SIDECAR_CREDENTIAL) return 'SIDECAR_CREDENTIAL_MISSING';
    return null;
  }
  return args.device ? null : 'PHONE_DEVICE_REQUIRED';
}

const env = loadEnv();
const search = typeof window === 'undefined' ? '' : window.location.search;
const ARGS = parseRendererArgs(search);

module.exports = {
  ARGS,
  env,
  SIDECAR_USER_ID,
  sidecarCredential: process.env.VOICE_SIDECAR_CREDENTIAL || '',
  roomPrefix: env.TRTC_ROOM_PREFIX || 'jax-',
  parseArgList,
  parseRendererArgs,
  validateStartup,
};
