'use strict';

const { publishRuntime } = require('../lib/sidecar-runtime-publish');

function send(message) {
  if (typeof process.send === 'function') process.send(message);
}

const runtimeDir = process.env.SIDECAR_RUNTIME_DIR;
if (!runtimeDir) {
  throw new Error('SIDECAR_RUNTIME_DIR is required');
}

const staged = publishRuntime({ runtimeDir });
send({ event: 'staged', runtimeRoot: staged.runtimeRoot, stagingDir: staged.stagingDir });

process.on('message', (message) => {
  if (message === 'exit') process.exit(0);
});
