import { describe, it, expect, vi, beforeEach } from "vitest";

import type { StreamQueryOptions, QueryRequest } from "../../api/sse";

// mock 网络层：用受控的 streamQuery 模拟 SSE 回放，REST 用 spy。
const streamQueryMock = vi.fn();
const cancelTaskMock = vi.fn();
const getSessionHistoryMock = vi.fn();
const getHistoryDetailMock = vi.fn();
const deleteSessionMock = vi.fn();

vi.mock("../../api/sse", () => ({
  streamQuery: (req: QueryRequest, opts: StreamQueryOptions) => streamQueryMock(req, opts),
}));
vi.mock("../../api/rest", () => ({
  cancelTask: (taskId: string) => cancelTaskMock(taskId),
  getSessionHistory: (sessionId: string) => getSessionHistoryMock(sessionId),
  getHistoryDetail: (id: number) => getHistoryDetailMock(id),
  deleteSession: (sessionId: string) => deleteSessionMock(sessionId),
}));

import { useChatStore } from "../chat";

// 取得纯净的初始 store（每个用例重置）。
beforeEach(() => {
  streamQueryMock.mockReset();
  cancelTaskMock.mockReset();
  getSessionHistoryMock.mockReset();
  getHistoryDetailMock.mockReset();
  deleteSessionMock.mockReset();
  useChatStore.setState({ turns: [], sending: false, controller: null, historyVersion: 0 });
});

describe("useChatStore.send", () => {
  it("正常链路：累积出一条 finished 的 turn", async () => {
    streamQueryMock.mockImplementation(async (_req: QueryRequest, opts: StreamQueryOptions) => {
      opts.onTaskId?.("server-task");
      opts.onTask?.({ task_id: "server-task", status: "started" });
      opts.onNode?.({
        task_id: "server-task",
        node: "sql_generator",
        data: { sql_plan: {}, generated_sql: "SELECT 1", chart_type: "bar" },
      } as never);
      opts.onTask?.({ task_id: "server-task", status: "finished" });
    });

    await useChatStore.getState().send("统计金额");

    const { turns, sending } = useChatStore.getState();
    expect(sending).toBe(false);
    expect(turns).toHaveLength(1);
    expect(turns[0].query).toBe("统计金额");
    expect(turns[0].status).toBe("finished");
    expect(turns[0].result.generatedSql).toBe("SELECT 1");
    expect(turns[0].taskId).toBe("server-task");
  });

  it("空白输入不发起请求", async () => {
    await useChatStore.getState().send("   ");
    expect(streamQueryMock).not.toHaveBeenCalled();
    expect(useChatStore.getState().turns).toHaveLength(0);
  });

  it("发送时携带 session_id 与本地 task_id", async () => {
    streamQueryMock.mockResolvedValue(undefined);
    await useChatStore.getState().send("q");
    const [req] = streamQueryMock.mock.calls[0];
    expect(req.session_id).toBe(useChatStore.getState().sessionId);
    expect(req.task_id).toMatch(/^task-/);
  });

  it("流异常（非取消）兜底为 error 状态", async () => {
    streamQueryMock.mockRejectedValue(new Error("boom"));
    await useChatStore.getState().send("q");
    expect(useChatStore.getState().turns[0].status).toBe("error");
  });
});

describe("useChatStore.cancel", () => {
  it("取消调用 cancelTask 并把当前 turn 置为 cancelled", async () => {
    // streamQuery 挂起直到 abort 触发。
    streamQueryMock.mockImplementation(
      (_req: QueryRequest, opts: StreamQueryOptions) =>
        new Promise<void>((resolve) => {
          opts.onTask?.({ task_id: "t", status: "started" });
          opts.signal?.addEventListener("abort", () => resolve());
        }),
    );
    cancelTaskMock.mockResolvedValue({ task_id: "x", cancelled: true });

    const sendPromise = useChatStore.getState().send("q");
    // 等待 turn 入列。
    await Promise.resolve();
    await useChatStore.getState().cancel();
    await sendPromise;

    expect(cancelTaskMock).toHaveBeenCalledTimes(1);
    const turn = useChatStore.getState().turns[0];
    expect(turn.status).toBe("cancelled");
    // 取消必须经过事件归约：进度条与 turn 状态一致，不再残留运行中节点。
    expect(turn.progress.overall).toBe("cancelled");
    expect(turn.progress.activeNode).toBeNull();
    expect(turn.progress.nodes.some((n) => n.status === "active")).toBe(false);
  });

  it("手动取消与 SSE cancelled 收敛到同一进度归约结果", async () => {
    // 路径 A：手动 cancel()。
    streamQueryMock.mockImplementation(
      (_req: QueryRequest, opts: StreamQueryOptions) =>
        new Promise<void>((resolve) => {
          opts.onTask?.({ task_id: "t", status: "started" });
          opts.signal?.addEventListener("abort", () => resolve());
        }),
    );
    cancelTaskMock.mockResolvedValue({ task_id: "x", cancelled: true });
    const p = useChatStore.getState().send("q");
    await Promise.resolve();
    await useChatStore.getState().cancel();
    await p;
    const manual = useChatStore.getState().turns[0].progress;

    // 路径 B：后端直接下发 SSE cancelled 事件。
    useChatStore.setState({ turns: [], sending: false, controller: null });
    streamQueryMock.mockImplementation((_req: QueryRequest, opts: StreamQueryOptions) => {
      opts.onTask?.({ task_id: "t", status: "started" });
      opts.onCancelled?.({ task_id: "t", cancelled: true });
      return Promise.resolve();
    });
    await useChatStore.getState().send("q");
    const sse = useChatStore.getState().turns[0].progress;

    expect(manual.overall).toBe(sse.overall);
    expect(manual.nodes.map((n) => n.status)).toEqual(sse.nodes.map((n) => n.status));
  });
});

describe("会话切换", () => {
  it("newSession 清空 turns 并换新 sessionId", () => {
    useChatStore.setState({ turns: [{ id: "x" } as never] });
    const old = useChatStore.getState().sessionId;
    useChatStore.getState().newSession();
    expect(useChatStore.getState().turns).toHaveLength(0);
    expect(useChatStore.getState().sessionId).not.toBe(old);
  });
});

describe("历史回看 openSession / removeSession", () => {
  const historyTurn = {
    id: 7,
    user_query: "按月统计金额",
    rewritten_query: "各月金额",
    generated_sql: "SELECT 1",
    tables: ["orders"],
    summary: "上升",
    chart_type: "line",
    row_count: 1,
    elapsed_ms: 2,
    trace_id: "tr",
    status: "success",
    created_at: "2026-06-28T00:00:00Z",
  };

  it("openSession 载入历史并用详情补全执行结果", async () => {
    getSessionHistoryMock.mockResolvedValue({ session_id: "s9", history: [historyTurn] });
    getHistoryDetailMock.mockResolvedValue({
      id: 7,
      session_id: "s9",
      user_query: "按月统计金额",
      generated_sql: "SELECT 1",
      summary: "上升",
      chart_type: "line",
      render_spec: { chart_type: "line", x: "m", y: ["v"], series: null, title: "t", options: {} },
      execution_result: { columns: ["m", "v"], rows: [{ m: "1月", v: 9 }], row_count: 1, elapsed_ms: 2, error: null },
      created_at: "2026-06-28T00:00:00Z",
    });

    await useChatStore.getState().openSession("s9");

    const { sessionId, turns } = useChatStore.getState();
    expect(sessionId).toBe("s9");
    expect(turns).toHaveLength(1);
    expect(turns[0].query).toBe("按月统计金额");
    expect(turns[0].result.executionResult?.row_count).toBe(1);
    expect(turns[0].result.renderSpec?.x).toBe("m");
  });

  it("openSession 中单条详情失败不影响列表回看", async () => {
    getSessionHistoryMock.mockResolvedValue({ session_id: "s9", history: [historyTurn] });
    getHistoryDetailMock.mockRejectedValue(new Error("404"));

    await useChatStore.getState().openSession("s9");

    const { turns } = useChatStore.getState();
    expect(turns).toHaveLength(1);
    expect(turns[0].result.generatedSql).toBe("SELECT 1"); // 列表字段仍在
    expect(turns[0].result.executionResult).toBeNull(); // 详情失败，无法重绘
  });

  it("removeSession 删除当前会话则切到新会话", async () => {
    deleteSessionMock.mockResolvedValue({ session_id: "cur", deleted: true });
    const current = useChatStore.getState().sessionId;
    await useChatStore.getState().removeSession(current);
    expect(deleteSessionMock).toHaveBeenCalledWith(current);
    expect(useChatStore.getState().sessionId).not.toBe(current);
    expect(useChatStore.getState().turns).toHaveLength(0);
  });

  it("removeSession 删除非当前会话仅触发历史刷新", async () => {
    deleteSessionMock.mockResolvedValue({ session_id: "other", deleted: true });
    const before = useChatStore.getState().historyVersion;
    const current = useChatStore.getState().sessionId;
    await useChatStore.getState().removeSession("other");
    expect(useChatStore.getState().sessionId).toBe(current);
    expect(useChatStore.getState().historyVersion).toBe(before + 1);
  });
});
