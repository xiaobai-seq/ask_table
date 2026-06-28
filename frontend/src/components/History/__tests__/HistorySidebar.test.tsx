import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// mock REST 模块：组件与 store 都从同一模块导入，单次 mock 即可覆盖两者。
const listSessionsMock = vi.fn();
const getSessionHistoryMock = vi.fn();
const getHistoryDetailMock = vi.fn();
const deleteSessionMock = vi.fn();

vi.mock("../../../api/rest", () => ({
  listSessions: () => listSessionsMock(),
  getSessionHistory: (id: string) => getSessionHistoryMock(id),
  getHistoryDetail: (id: number) => getHistoryDetailMock(id),
  deleteSession: (id: string) => deleteSessionMock(id),
  cancelTask: vi.fn(),
}));

import HistorySidebar from "../HistorySidebar";

const sessions = [
  { session_id: "s1", title: "按月份统计订单金额", created_at: "2026-06-28T01:00:00Z", updated_at: "2026-06-28T02:00:00Z", turn_count: 3 },
  { session_id: "s2", title: "各地区销售额占比", created_at: "2026-06-27T01:00:00Z", updated_at: "2026-06-27T02:00:00Z", turn_count: 5 },
];

beforeEach(() => {
  listSessionsMock.mockReset();
  getSessionHistoryMock.mockReset();
  getHistoryDetailMock.mockReset();
  deleteSessionMock.mockReset();
  listSessionsMock.mockResolvedValue({ sessions });
  getSessionHistoryMock.mockResolvedValue({ session_id: "s1", history: [] });
  deleteSessionMock.mockResolvedValue({ session_id: "s1", deleted: true });
});

describe("HistorySidebar", () => {
  it("挂载后拉取并渲染会话列表", async () => {
    render(<HistorySidebar />);
    expect(await screen.findByText("按月份统计订单金额")).toBeInTheDocument();
    expect(screen.getByText("各地区销售额占比")).toBeInTheDocument();
    // 描述含轮次数。
    expect(screen.getByText(/3 轮/)).toBeInTheDocument();
  });

  it("列表为空时展示空态", async () => {
    listSessionsMock.mockResolvedValue({ sessions: [] });
    render(<HistorySidebar />);
    expect(await screen.findByText("暂无历史会话")).toBeInTheDocument();
  });

  it("点击会话触发回看（拉取该会话历史）", async () => {
    const user = userEvent.setup();
    render(<HistorySidebar />);
    await user.click(await screen.findByText("各地区销售额占比"));
    await waitFor(() => expect(getSessionHistoryMock).toHaveBeenCalledWith("s2"));
  });

  it("删除会话需二次确认后调用 deleteSession", async () => {
    const user = userEvent.setup();
    render(<HistorySidebar />);
    await screen.findByText("按月份统计订单金额");
    const delButtons = screen.getAllByLabelText("删除会话");
    await user.click(delButtons[0]);
    // Popconfirm 确认弹层中的「删除」按钮（AntD 会在两汉字间插空格，匹配时忽略空白）。
    const confirmBtn = await screen.findByRole("button", {
      name: (name: string) => name.replace(/\s/g, "") === "删除",
    });
    await user.click(confirmBtn);
    await waitFor(() => expect(deleteSessionMock).toHaveBeenCalledWith("s1"));
  });
});
