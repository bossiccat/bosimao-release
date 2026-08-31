// 为模拟器配对测试签一个新配对码
const BASE = process.env.JAX_VOICE_BASE || 'https://jinhong-d2g55ycl591208475-1436773060.ap-shanghai.app.tcloudbase.com/api/v1/voice';
const fs = require('fs');
const OWNER = fs.readFileSync('C:/Users/Administrator/WorkBuddy/监视app/.env', 'utf8').match(/^VOICE_OWNER_CREDENTIAL=(.+)$/m)[1].trim();
(async () => {
  const r = await fetch(BASE + '/devices/pairing-code', { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + OWNER }, body: JSON.stringify({ platform: 'android', device_name_hint: 'emulator-pair-test' }) });
  const j = await r.json();
  console.log(j.data.pairing_code);
})();
