// 会话状态管理（Zustand）。负责：发起流式查询、累积每轮结果、取消、加载历史回看。
// 与网络的耦合点仅有 streamQuery / cancelTask，便于在测试中 mock。

import { create } from "zustand";

import {
  cancelTask,
  deleteSession as deleteSessionApi,
  getHistoryDetail,
  getSessionHistory,
} from "../api/rest";
import { streamQuery } from "../api/sse";
import type { SSEEvent } from "../api/types";
import {
  applyEventToTurn,
  createTurn,
  enrichTurnWithDetail,
  historyTurnToChatTurn,
  type ChatTurn,
} from "./turn";

// 生成本地唯一 id（task_id 前端自带，便于在收到响应头前就能取消）。
function genId(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function newSessionId(): string {
  return genId("session");
}

interface ChatState {
  sessionId: string;
  turns: ChatTurn[];
  sending: boolean;
  controller: AbortController | null;
  // 每次有轮次完成时自增，供历史侧边栏感知刷新。
  historyVersion: number;

  send: (query: string) => Promise<void>;
  cancel: () => Promise<void>;
  newSession: () => void;
  switchSession: (sessionId: string) => void;
  loadTurns: (sessionId: string, turns: ChatTurn[]) => void;
  // 历史回看：拉取某会话历史并载入对话区。
  openSession: (sessionId: string) => Promise<void>;
  // 删除会话：若删除的是当前会话则回到一个新会话。
  removeSession: (sessionId: string) => Promise<void>;
}

export const useChatStore = create<ChatState>((set, get) => {
  // 按 id 不可变更新某个 turn。
  const patchTurn = (id: string, fn: (t: ChatTurn) => ChatTurn) =>
    set((state) => ({ turns: state.turns.map((t) => (t.id === id ? fn(t) : t)) }));

  // 把一条 SSE 事件应用到指定 turn。
  const applyEvent = (id: string, event: SSEEvent) =>
    patchTurn(id, (t) => applyEventToTurn(t, event));

  return {
    sessionId: newSessionId(),
    turns: [],
    sending: false,
    controller: null,
    historyVersion: 0,

    async send(query: string) {
      const trimmed = query.trim();
      if (!trimmed || get().sending) return;

      const turnId = genId("turn");
      const taskId = genId("task");
      const { sessionId } = get();
      const controller = new AbortController();

      // 先放入一条 streaming 的 turn（含本地 taskId，便于即时取消）。
      set((state) => ({
        turns: [...state.turns, { ...createTurn(turnId, trimmed), taskId }],
        sending: true,
        controller,
      }));

      try {
        await streamQuery(
          { query: trimmed, session_id: sessionId, task_id: taskId },
          {
            signal: controller.signal,
            onTaskId: (tid) => patchTurn(turnId, (t) => ({ ...t, taskId: tid })),
            onTask: (payload) => applyEvent(turnId, { type: "task", payload }),
            onNode: (payload) => applyEvent(turnId, { type: payload.node, payload } as SSEEvent),
            onCancelled: (payload) => applyEvent(turnId, { type: "cancelled", payload }),
            onError: (payload) => applyEvent(turnId, { type: "error", payload }),
          },
        );
      } catch {
        // 主动取消会走到这里（AbortError）；非取消的网络异常兜底为 error 状态。
        if (!controller.signal.aborted) {
          patchTurn(turnId, (t) => ({
            ...t,
            status: "error",
            error: { code: "network_error", message: "网络连接中断，请重试", trace_id: "" },
          }));
        }
      } finally {
        set((state) => ({
          sending: false,
          controller: null,
          historyVersion: state.historyVersion + 1,
        }));
      }
    },

    async cancel() {
      const { controller, turns } = get();
      // 找到当前正在流式处理的 turn。
      const streaming = [...turns].reverse().find((t) => t.status === "streaming");
      if (streaming?.taskId) {
        try {
          await cancelTask(streaming.taskId);
        } catch {
          // 取消接口失败不阻断：本地仍中止流并置为 cancelled。
        }
        patchTurn(streaming.id, (t) => ({ ...t, status: "cancelled" }));
      }
      controller?.abort();
    },

    newSession() {
      get().controller?.abort();
      set({ sessionId: newSessionId(), turns: [], sending: false, controller: null });
    },

    switchSession(sessionId: string) {
      get().controller?.abort();
      set({ sessionId, turns: [], sending: false, controller: null });
    },

    loadTurns(sessionId: string, turns: ChatTurn[]) {
      get().controller?.abort();
      set({ sessionId, turns, sending: false, controller: null });
    },

    async openSession(sessionId: string) {
      const resp = await getSessionHistory(sessionId);
      const turns = resp.history.map(historyTurnToChatTurn);
      // 先载入列表（含 query/SQL/摘要），保证回看即时可见。
      get().controller?.abort();
      set({ sessionId, turns, sending: false, controller: null });

      // 再并发补全各轮详情（render_spec / execution_result）以重绘图表；
      // 单条详情失败不影响整体回看，期间若已切换会话则放弃本次结果。
      const details = await Promise.all(
        resp.history.map((h) => getHistoryDetail(h.id).catch(() => null)),
      );
      const enriched = turns.map((t, i) => (details[i] ? enrichTurnWithDetail(t, details[i]!) : t));
      set((state) => (state.sessionId === sessionId ? { turns: enriched } : {}));
    },

    async removeSession(sessionId: string) {
      await deleteSessionApi(sessionId);
      // 删除当前会话则切到全新会话；否则仅触发历史刷新。
      if (get().sessionId === sessionId) {
        get().newSession();
      } else {
        set((state) => ({ historyVersion: state.historyVersion + 1 }));
      }
    },
  };
});
