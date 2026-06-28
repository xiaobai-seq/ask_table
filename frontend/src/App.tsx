import { Button, Layout, Space, Typography } from "antd";
import { PlusOutlined } from "@ant-design/icons";

import Chat from "./pages/Chat";
import { useChatStore } from "./store/chat";

const { Header, Content } = Layout;

export default function App() {
  const newSession = useChatStore((s) => s.newSession);

  return (
    <Layout style={{ height: "100%" }}>
      <Header
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          borderBottom: "1px solid #eef0f4",
        }}
      >
        <Typography.Title level={4} style={{ margin: 0 }}>
          Text2SQL · 智能数据问答
        </Typography.Title>
        <Space>
          <Button icon={<PlusOutlined />} onClick={() => newSession()}>
            新会话
          </Button>
        </Space>
      </Header>
      <Content style={{ height: "calc(100% - 64px)" }}>
        <Chat />
      </Content>
    </Layout>
  );
}
