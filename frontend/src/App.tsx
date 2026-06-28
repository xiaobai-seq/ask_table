import { Layout, Typography } from "antd";

import Chat from "./pages/Chat";
import HistorySidebar from "./components/History/HistorySidebar";

const { Header, Content, Sider } = Layout;

export default function App() {
  return (
    <Layout style={{ height: "100%" }}>
      <Header
        style={{
          display: "flex",
          alignItems: "center",
          borderBottom: "1px solid #eef0f4",
        }}
      >
        <Typography.Title level={4} style={{ margin: 0 }}>
          Text2SQL · 智能数据问答
        </Typography.Title>
      </Header>
      <Layout>
        <Sider width={280} theme="light" style={{ borderRight: "1px solid #eef0f4" }}>
          <HistorySidebar />
        </Sider>
        <Content style={{ height: "calc(100vh - 64px)" }}>
          <Chat />
        </Content>
      </Layout>
    </Layout>
  );
}
