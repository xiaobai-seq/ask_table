// REST 客户端：取消、会话与历史（契约 §2、§3）。统一以 API_BASE_URL 为前缀。

import { API_BASE_URL } from "./config";
import type {
  ApiError,
  AppConfig,
  CancelResponse,
  DeleteSessionResponse,
  HistoryDetail,
  SessionHistoryResponse,
  SessionListResponse,
} from "./types";

// 统一请求封装：非 2xx 时抛出结构化错误（尽量解析后端 {code,message,trace_id}）。
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${API_BASE_URL}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!resp.ok) {
    let err: ApiError = {
      code: `http_${resp.status}`,
      message: `请求失败 (HTTP ${resp.status})`,
      trace_id: "",
    };
    try {
      err = { ...err, ...((await resp.json()) as Partial<ApiError>) };
    } catch {
      // 忽略 JSON 解析失败，沿用默认错误体。
    }
    throw err;
  }
  return (await resp.json()) as T;
}

// 取消任务：POST /cancel/{task_id}。
export function cancelTask(taskId: string): Promise<CancelResponse> {
  return request<CancelResponse>(`/cancel/${encodeURIComponent(taskId)}`, { method: "POST" });
}

// 会话列表：GET /sessions。
export function listSessions(): Promise<SessionListResponse> {
  return request<SessionListResponse>(`/sessions`);
}

// 单会话历史：GET /sessions/{id}/history。
export function getSessionHistory(sessionId: string): Promise<SessionHistoryResponse> {
  return request<SessionHistoryResponse>(`/sessions/${encodeURIComponent(sessionId)}/history`);
}

// 单条历史明细（含 render_spec / execution_result，便于回看重绘）：GET /history/{id}。
export function getHistoryDetail(id: number): Promise<HistoryDetail> {
  return request<HistoryDetail>(`/history/${id}`);
}

// 删除会话：DELETE /sessions/{id}。
export function deleteSession(sessionId: string): Promise<DeleteSessionResponse> {
  return request<DeleteSessionResponse>(`/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
}

// 应用配置：GET /config。用于场景化示例问题等轻量 UI 配置。
export function getAppConfig(): Promise<AppConfig> {
  return request<AppConfig>(`/config`);
}
