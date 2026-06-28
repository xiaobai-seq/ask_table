import { useCallback, useEffect, useState } from "react";
import { Button, Empty, List, Popconfirm, Spin, Tooltip, Typography } from "antd";
import { DeleteOutlined, PlusOutlined, ReloadOutlined } from "@ant-design/icons";

import { listSessions } from "../../api/rest";
import type { SessionSummary } from "../../api/types";
import { useChatStore } from "../../store/chat";

// 把 ISO 时间转成简洁的本地展示（失败则原样返回）。
function formatTime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

// 历史侧边栏：会话列表 + 点击回看 + 删除。
// 列表随 historyVersion 自动刷新（每轮查询完成 / 删除后都会变化）。
export default function HistorySidebar() {
  const sessionId = useChatStore((s) => s.sessionId);
  const historyVersion = useChatStore((s) => s.historyVersion);
  const openSession = useChatStore((s) => s.openSession);
  const removeSession = useChatStore((s) => s.removeSession);
  const newSession = useChatStore((s) => s.newSession);

  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const resp = await listSessions();
      setSessions(resp.sessions);
    } catch {
      // 后端不可用时列表置空，不打断主流程。
      setSessions([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh, historyVersion]);

  const handleDelete = async (id: string) => {
    await removeSession(id);
    await refresh();
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "12px 16px",
          borderBottom: "1px solid #eef0f4",
        }}
      >
        <Typography.Text strong>历史会话</Typography.Text>
        <span>
          <Tooltip title="刷新">
            <Button type="text" size="small" icon={<ReloadOutlined />} onClick={() => void refresh()} />
          </Tooltip>
          <Tooltip title="新会话">
            <Button type="text" size="small" icon={<PlusOutlined />} onClick={() => newSession()} />
          </Tooltip>
        </span>
      </div>

      <div style={{ flex: 1, overflowY: "auto" }}>
        {loading && sessions.length === 0 ? (
          <div style={{ textAlign: "center", padding: 24 }}>
            <Spin />
          </div>
        ) : sessions.length === 0 ? (
          <Empty style={{ marginTop: 48 }} image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无历史会话" />
        ) : (
          <List
            dataSource={sessions}
            renderItem={(s) => {
              const active = s.session_id === sessionId;
              return (
                <List.Item
                  data-testid="session-item"
                  data-active={active}
                  style={{
                    padding: "10px 16px",
                    cursor: "pointer",
                    background: active ? "#eef2ff" : undefined,
                  }}
                  onClick={() => void openSession(s.session_id)}
                  actions={[
                    <Popconfirm
                      key="del"
                      title="删除该会话？"
                      description="该会话的所有历史记录将被删除。"
                      okText="删除"
                      cancelText="取消"
                      okButtonProps={{ danger: true }}
                      onConfirm={() => void handleDelete(s.session_id)}
                    >
                      <Button
                        type="text"
                        size="small"
                        danger
                        aria-label="删除会话"
                        icon={<DeleteOutlined />}
                        onClick={(e) => e.stopPropagation()}
                      />
                    </Popconfirm>,
                  ]}
                >
                  <List.Item.Meta
                    title={
                      <Typography.Text ellipsis style={{ maxWidth: 180 }}>
                        {s.title || "未命名会话"}
                      </Typography.Text>
                    }
                    description={
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        {formatTime(s.updated_at)} · {s.turn_count} 轮
                      </Typography.Text>
                    }
                  />
                </List.Item>
              );
            }}
          />
        )}
      </div>
    </div>
  );
}
