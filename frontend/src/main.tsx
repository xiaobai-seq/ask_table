import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";

import App from "./App";
import { enterpriseTheme } from "./theme";
import "antd/dist/reset.css";
import "./index.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ConfigProvider locale={zhCN} theme={enterpriseTheme}>
      <App />
    </ConfigProvider>
  </StrictMode>,
);
