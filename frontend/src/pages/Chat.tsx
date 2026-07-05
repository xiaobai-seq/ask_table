import { useEffect, useRef, useState } from "react";
import { Button, Empty, Input, Space, Tag } from "antd";
import { SendOutlined, StopOutlined } from "@ant-design/icons";

import { getAppConfig } from "../api/rest";
import TurnView from "../components/Chat/TurnView";
import { useChatStore } from "../store/chat";

const DEFAULT_EXAMPLES = ["按月份统计订单金额趋势", "各地区销售额占比", "销量最高的前 10 个商品"];

// 主问答页：对话式气泡 + 输入框 + 取消按钮，状态全部来自 Zustand store。
export default function Chat() {
  const turns = useChatStore((s) => s.turns);
  const sending = useChatStore((s) => s.sending);
  const send = useChatStore((s) => s.send);
  const cancel = useChatStore((s) => s.cancel);

  const [text, setText] = useState("");
  const [examples, setExamples] = useState(DEFAULT_EXAMPLES);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    void getAppConfig()
      .then((config) => {
        if (config.example_queries.length > 0) {
          setExamples(config.example_queries);
        }
      })
      .catch(() => {
        setExamples(DEFAULT_EXAMPLES);
      });
  }, []);

  // 新消息到达时自动滚动到底部。
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns]);

  const submit = () => {
    const q = text.trim();
    if (!q || sending) return;
    setText("");
    void send(q);
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      {/* 消息列表区（可滚动） */}
      <div style={{ flex: 1, overflowY: "auto", padding: "24px 24px 8px" }}>
        {turns.length === 0 ? (
          <div style={{ marginTop: 80 }}>
            <Empty description="开始你的第一个数据问题">
              <Space wrap style={{ justifyContent: "center" }}>
                {examples.map((ex) => (
                  <Tag.CheckableTag key={ex} checked={false} onChange={() => void send(ex)}>
                    {ex}
                  </Tag.CheckableTag>
                ))}
              </Space>
            </Empty>
          </div>
        ) : (
          turns.map((turn) => (
            <TurnView key={turn.id} turn={turn} sending={sending} onClarify={(opt) => void send(opt)} />
          ))
        )}
        <div ref={bottomRef} />
      </div>

      {/* 输入区 */}
      <div style={{ borderTop: "1px solid #eef0f4", padding: 16, background: "#fff" }}>
        <Space.Compact style={{ width: "100%" }}>
          <Input.TextArea
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={`用自然语言提问，例如：${examples[0] ?? DEFAULT_EXAMPLES[0]}`}
            autoSize={{ minRows: 1, maxRows: 4 }}
            // 回车发送，Shift+回车换行。
            onPressEnter={(e) => {
              if (!e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
            disabled={sending}
          />
          {sending ? (
            <Button danger icon={<StopOutlined />} onClick={() => void cancel()}>
              取消
            </Button>
          ) : (
            <Button type="primary" icon={<SendOutlined />} onClick={submit}>
              发送
            </Button>
          )}
        </Space.Compact>
      </div>
    </div>
  );
}
