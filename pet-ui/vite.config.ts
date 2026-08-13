import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Tauri 需要固定端口 + 明确 base
export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: {
    port: 5173,
    strictPort: true,
    watch: {
      ignored: ["**/src-tauri/**"],
    },
  },
  build: {
    target: "es2021",
    outDir: "dist",
    assetsDir: "assets",
    // 2026-08-13：本机构建环境对 Node fs 递归删除有安全拦截（safe-delete shim），
    // vite 每次 emptyDir(dist) 均被拒。关闭自动清空：vite 覆盖写入同名文件，
    // 旧 hash 资源残留不影响产物正确性（index.html 引用最新 hash）。
    emptyOutDir: false,
  },
});
