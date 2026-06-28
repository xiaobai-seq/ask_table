import { Layout, Typography } from "antd";

const { Header, Content } = Layout;

// 脚手架阶段的占位首页，后续 Task 3.5 会替换为完整问答页。
export default function App() {
  return (
    <Layout style={{ minHeight: "100%" }}>
      <Header style={{ display: "flex", alignItems: "center" }}>
        <Typography.Title level={4} style={{ margin: 0 }}>
          Text2SQL · 智能数据问答
        </Typography.Title>
      </Header>
      <Content style={{ padding: 24 }}>
        <Typography.Paragraph>前端脚手架已就绪。</Typography.Paragraph>
      </Content>
    </Layout>
  );
}
