import { describe, it, expect, vi, beforeEach } from "vitest";

import type { StreamQueryOptions, QueryRequest } from "../../api/sse";

// mock 网络层：用受控的 streamQuery 模拟 SSE 回放。
const streamQueryMock = vi.fn();
const cancelTaskMock = vi.fn();

vi.mock("../../api/sse", () => ({
  streamQuery: (req: QueryRequest, opts: StreamQueryOptions) => streamQueryMock(req, opts),
}));
vi.mock("../../api/rest", () => ({
  cancelTask: (taskId: string) => cancelTaskMock(taskId),
}));

import { useChatStore } from "../chat";

// 取得纯净的初始 store（每个用例重置）。
beforeEach(() => {
  streamQueryMock.mockReset();
  cancelTaskMock.mockReset();
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
    expect(useChatStore.getState().turns[0].status).toBe("cancelled");
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
