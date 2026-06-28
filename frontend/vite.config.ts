import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// 后端地址可通过环境变量覆盖，默认开发期直连本地 8000 端口。
// dev server 将 /api/* 代理到后端并去掉 /api 前缀，因此前端代码统一以 /api 为 baseURL，
// 既能走代理（规避浏览器跨域），后端也无需强依赖 CORS。
const BACKEND_TARGET = process.env.VITE_BACKEND_TARGET ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: BACKEND_TARGET,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
