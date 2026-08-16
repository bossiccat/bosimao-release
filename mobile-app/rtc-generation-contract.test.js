'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const source = fs.readFileSync(
  path.join(__dirname, 'app/src/main/java/com/jax/voice/net/RtcClient.kt'),
  'utf8'
);

test('each RTC enter attempt owns an identity-bound listener', () => {
  assert.match(source, /val listener = createAttemptListener \{ current \}[\s\S]*EnterAttempt\(generation = generation, listener = listener\)/);
  assert.doesNotMatch(source, /private val listener\s*=\s*object\s*:\s*TRTCCloudListener/);
  assert.match(source, /currentAttempt\(\)\.takeIf\(::isCurrentAttempt\)/);
});

test('release does not force lazy cloud creation', () => {
  assert.match(source, /private var cloud: TRTCCloud\? = null/);
  assert.doesNotMatch(source, /private val cloud: TRTCCloud by lazy/);
  assert.match(source, /if \(released\) return/);
});

test('attempt initialization rolls back before publishing failure', () => {
  assert.match(source, /catch \(t: Throwable\) \{[\s\S]*rollbackAttempt\(current\)[\s\S]*onSessionFailure\(generation, "engine_init"/);
});

test('SDK ownership and release teardown share one operation boundary', () => {
  assert.match(source, /private val sdkOperationLock = Any\(\)/);
  assert.match(source, /synchronized\(sdkOperationLock\) \{[\s\S]*engine\.addListener\(current\.listener\)[\s\S]*engine\.enterRoom/s);
  assert.match(source, /synchronized\(sdkOperationLock\) \{[\s\S]*current\?\.let \{[\s\S]*destroyEngine\(\)/s);
});

test('release exposes a claimed barrier before waiting for SDK ownership', () => {
  assert.match(source, /released = true[\s\S]*onReleaseClaimed\(\)[\s\S]*synchronized\(sdkOperationLock\)/s);
});
