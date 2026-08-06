// logger.js —— 控制台 + 本地文件双写（无头环境渲染进程 stdout 不可靠；按角色分文件）
const path = require('path');
const fs = require('fs');

function makeLogger(tag, file) {
  const LOG_FILE = path.resolve(__dirname, 'logs', file || `${tag}.log`);
  try {
    fs.mkdirSync(path.dirname(LOG_FILE), { recursive: true });
    fs.writeFileSync(LOG_FILE, '');
  } catch (e) {
    /* 写失败不阻塞 */
  }
  return function log(scope, msg) {
    const line = `[${new Date().toISOString()}] [${scope}] ${msg}`;
    console.log(line);
    try {
      fs.appendFileSync(LOG_FILE, line + '\n');
    } catch (e) {
      /* ignore */
    }
  };
}

module.exports = makeLogger;
