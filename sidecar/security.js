'use strict';

const crypto = require('crypto');

function controlPlaneHeaders({ credential } = {}) {
  const token = credential;
  if (!token) throw new Error('sidecar credential unavailable');
  return {
    'Content-Type': 'application/json',
    Authorization: `Bearer ${token}`,
    'X-Request-Nonce': crypto.randomBytes(32).toString('hex'),
  };
}

module.exports = { controlPlaneHeaders };
