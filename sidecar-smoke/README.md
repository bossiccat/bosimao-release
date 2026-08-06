# jax-rtc-sidecar-smoke（Phase A 冒烟环境）

TRTC Electron SDK 冒烟环境（R1 gate：PCM 帧确认 + 哑对端进房）。结论见 `docs/rtc-rebuild/R1-SMOKE.md`。

## 文件

| 文件 | 作用 |
|---|---|
| `main.js` | Electron 主进程：隐藏窗口 + 传参 |
| `index.html` | 渲染进程壳（DOM 环境必需，SDK 需在渲染进程运行） |
| `rtc-renderer.js` | 渲染进程逻辑：进房 / PCM 回调注册 / 远端互见日志 |
| `usersig.js` | Node 版 TLSSigAPIv2（对齐官方 Node 实现，供 sidecar 本地签发） |
| `package.json` | `trtc-electron-sdk@13.3.801` + `electron@31.7.7`（精确版本，禁 latest） |

## 运行

```bash
# 依赖（官方源 + fresh cache；本机 npm 10.9.7 safe-delete 有坑，见 R1-SMOKE.md §4）
npm install --registry=https://registry.npmjs.org --legacy-peer-deps --cache=./.npm-cache-smoke

# 哑对端进房（SecretKey 从项目根 .env 读取，不落代码）
unset ELECTRON_RUN_AS_NODE
node_modules/electron/dist/electron.exe --no-sandbox --disable-gpu . --device=smoke-dev-1 --user=jax-pc-sidecar --hold=40

# 本地双端回环（验证远端互见）
node_modules/electron/dist/electron.exe --no-sandbox --disable-gpu . --device=loop-dev-9 --user=jax-pc-sidecar --hold=60 &
node_modules/electron/dist/electron.exe --no-sandbox --disable-gpu . --device=loop-dev-9 --user=pc-phone --hold=50 &
```

日志：`logs/smoke-<userId>.log`。

> 无头环境：Electron 需 `--no-sandbox --disable-gpu`；`ELECTRON_RUN_AS_NODE=1` 会导致按纯 Node 运行而报 `document is not defined`。
