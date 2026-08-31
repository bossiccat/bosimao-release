// logger.js —— 控制台 + 本地文件双写（无头环境渲染进程 stdout 不可靠；按角色分文件）
const path = require('path');
const fs = require('fs');

function makeLogger(tag, file) {
  // RP-07 P0（2026-09-01）：打包态 sidecar 从 immutable generation 目录运行，
  // 写 __dirname/logs 会污染 generation 并破坏完整性闭集校验。
  // 宿主（main.rs setup）注入 JAX_SIDECAR_LOG_DIR 时写 app log dir；
  // 未注入（开发态直接 node main.js）保持旧的 __dirname/logs 行为。
  const baseDir = process.env.JAX_SIDECAR_LOG_DIR || path.join(__dirname, 'logs');
  const LOG_FILE = path.resolve(baseDir, file || `${tag}.log`);
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
