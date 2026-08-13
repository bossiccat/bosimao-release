'use strict';

const EXIT_CHANNEL = 'voice-sidecar:lifecycle-exit';
const EXIT_CODES = Object.freeze({ controlled: 0, fatal: 2 });

function validPayload(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return false;
  const keys = Object.keys(payload);
  return keys.length === 1 && keys[0] === 'kind' && Object.hasOwn(EXIT_CODES, payload.kind);
}

function createExitArbiter(exit) {
  let verdict;
  return {
    decide(payload) {
      if (verdict !== undefined) return verdict;
      verdict = validPayload(payload) ? EXIT_CODES[payload.kind] : EXIT_CODES.fatal;
      exit(verdict);
      return verdict;
    },
    verdict() { return verdict; },
  };
}

function requestRendererExit(kind) {
  const { ipcRenderer } = require('electron');
  ipcRenderer.send(EXIT_CHANNEL, { kind });
}

module.exports = { EXIT_CHANNEL, createExitArbiter, requestRendererExit, validPayload };
