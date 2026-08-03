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
  },
});
