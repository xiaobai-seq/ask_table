// 前端统一以 /api 作为 baseURL：dev server 会把 /api/* 代理到后端并去掉前缀，
// 既规避浏览器跨域，也让后端无需强依赖 CORS。可通过 VITE_API_BASE_URL 覆盖（如直连场景）。
export const API_BASE_URL =
  (typeof import.meta !== "undefined" && import.meta.env?.VITE_API_BASE_URL) || "/api";
