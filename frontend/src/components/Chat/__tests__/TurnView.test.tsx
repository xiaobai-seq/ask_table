import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import TurnView from "../TurnView";
import { createTurn, applyEventToTurn } from "../../../store/turn";
import type { SSEEvent } from "../../../api/types";

// AntD 会在两个汉字按钮文本间插入空格，匹配时忽略空白。
function buttonByText(text: string) {
  return (name: string) => name.replace(/\s/g, "") === text.replace(/\s/g, "");
}

// 构造一个处于澄清态的 turn。
function clarifyingTurn() {
  return applyEventToTurn(createTurn("t1", "按地区统计销售额"), {
    type: "schema_inspector",
    payload: {
      task_id: "x",
      node: "schema_inspector",
      data: {
        rewritten_query: "各地区销售额",
        db_info: [],
        retrieval_hits: [],
        clarification: { question: "你指的是哪个地区?", options: ["华北", "华南"], reason: "地区不明确" },
        trace_id: "tr",
      },
    },
  } as SSEEvent);
}

describe("TurnView 澄清卡片", () => {
  it("查询进行中（sending）时澄清选项不可点击", () => {
    render(<TurnView turn={clarifyingTurn()} sending onClarify={() => {}} />);
    expect(screen.getByRole("button", { name: buttonByText("华北") })).toBeDisabled();
  });

  it("空闲时澄清选项可点击并回调 onClarify", async () => {
    const onClarify = vi.fn();
    const user = userEvent.setup();
    render(<TurnView turn={clarifyingTurn()} sending={false} onClarify={onClarify} />);
    const btn = screen.getByRole("button", { name: buttonByText("华南") });
    expect(btn).not.toBeDisabled();
    await user.click(btn);
    expect(onClarify).toHaveBeenCalledWith("华南");
  });
});
