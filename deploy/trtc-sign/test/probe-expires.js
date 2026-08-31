const https = require('https');
const HOST = process.env.JAX_VOICE_HOST || 'jinhong-d2g55ycl591208475-1436773060.ap-shanghai.app.tcloudbase.com';
const path = '/api/v1/voice/session';
const body = JSON.stringify({ device_id: 'probe-device', nonce: 'probe-' + Date.now() });
const req = https.request({ host: HOST, method: 'POST', path, headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer invalid.probe', 'Content-Length': Buffer.byteLength(body) } }, (res) => {
  let data = '';
  res.on('data', c => data += c);
  res.on('end', () => { console.log('status:', res.statusCode); console.log('BODY:', data.slice(0, 600)); });
});
req.on('error', e => console.error('ERR:', e.message));
req.write(body);
req.end();
