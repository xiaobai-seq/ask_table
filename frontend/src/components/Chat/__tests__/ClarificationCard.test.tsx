import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import ClarificationCard from "../ClarificationCard";
import type { Clarification } from "../../../api/types";

const clarification: Clarification = {
  question: "你指的是哪个时间范围?",
  options: ["最近 7 天", "本月", "今年"],
  reason: "问题中的时间不明确",
};

// AntD 会在两个汉字按钮文本间自动插入空格（本月 -> 本 月），匹配时忽略空白。
function buttonByText(text: string) {
  return (name: string) => name.replace(/\s/g, "") === text.replace(/\s/g, "");
}

describe("ClarificationCard", () => {
  it("渲染问题、原因与所有候选项", () => {
    render(<ClarificationCard clarification={clarification} onSelect={() => {}} />);
    expect(screen.getByText("你指的是哪个时间范围?")).toBeInTheDocument();
    expect(screen.getByText("问题中的时间不明确")).toBeInTheDocument();
    for (const opt of clarification.options) {
      expect(screen.getByRole("button", { name: buttonByText(opt) })).toBeInTheDocument();
    }
  });

  it("点击候选项回调 onSelect 并带上选项文本", async () => {
    const onSelect = vi.fn();
    const user = userEvent.setup();
    render(<ClarificationCard clarification={clarification} onSelect={onSelect} />);
    await user.click(screen.getByRole("button", { name: buttonByText("本月") }));
    expect(onSelect).toHaveBeenCalledWith("本月");
  });

  it("disabled 时候选项不可点击", async () => {
    const onSelect = vi.fn();
    const user = userEvent.setup();
    render(<ClarificationCard clarification={clarification} disabled onSelect={onSelect} />);
    await user.click(screen.getByRole("button", { name: buttonByText("今年") }));
    expect(onSelect).not.toHaveBeenCalled();
  });
});
