'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const { main_handler: mainHandler } = require('../index');

async function post(path, body) {
  const response = await mainHandler({
    httpMethod: 'POST',
    path,
    headers: {},
    body: JSON.stringify(body),
  }, {});
  return { response, payload: JSON.parse(response.body) };
}

test('POST session terminate is recognized instead of returning generic 40500', async () => {
  const { response, payload } = await post(
    '/api/v1/voice/sessions/session-1/terminate',
    {
      session_id: 'session-1',
      device_id: 'device-1',
      room_id: 'room-1',
      generation: 0,
      request_id: 'request-1',
      reason: 'user_stop',
      requested_at: '2026-08-25T00:00:00Z',
    }
  );

  assert.notEqual(payload.code, 40500, JSON.stringify({ response, payload }));
});

test('POST termination acknowledgement is recognized instead of returning generic 40500', async () => {
  const { response, payload } = await post(
    '/api/v1/voice/sessions/session-1/termination/termination-1/acknowledgements',
    {
      acknowledgement: 'bridge_drained_closed',
      result: 'confirmed',
      session_id: 'session-1',
      device_id: 'device-1',
      room_id: 'room-1',
      generation: 0,
      reported_at: '2026-08-25T00:00:00Z',
    }
  );

  assert.notEqual(payload.code, 40500, JSON.stringify({ response, payload }));
});
