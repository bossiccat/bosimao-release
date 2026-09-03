import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// 测试专用配置（与 vite.config.ts 分离，避免污染 Tauri 构建链）
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    // 只扫第一方测试；src-tauri/binaries/sidecar-copy-probe 是 sidecar 探针样本（含第三方包测试），必须排除
    include: ["src/test/**/*.{test,spec}.{ts,tsx}"],
  },
});
