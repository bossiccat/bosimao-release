'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { publishCurrentPointer } = require('../lib/sidecar-runtime-publish');

const runtimeDir = process.env.SIDECAR_RUNTIME_DIR;
const pointer = JSON.parse(process.env.SIDECAR_POINTER_JSON);
const mode = process.env.SIDECAR_PUBLISH_MODE || 'publish';
if (!runtimeDir) throw new Error('SIDECAR_RUNTIME_DIR is required');

if (mode === 'crash-before-replace') {
  const currentPath = path.join(runtimeDir, 'current.json');
  const temporaryPath = `${currentPath}.${process.pid}.tmp`;
  const bytes = Buffer.from(`${JSON.stringify(pointer)}\n`);
  const descriptor = fs.openSync(temporaryPath, 'wx');
  try {
    fs.writeSync(descriptor, bytes);
    fs.fsyncSync(descriptor);
  } finally {
    fs.closeSync(descriptor);
  }
  process.kill(process.pid, 'SIGKILL');
} else {
  publishCurrentPointer({ runtimeDir, pointer });
}
