import type { ThemeConfig } from "antd";

// 企业浅色主题：克制的主色、稍紧凑的圆角，整体偏冷静的蓝灰调，适合数据类后台。
export const enterpriseTheme: ThemeConfig = {
  token: {
    colorPrimary: "#2f54eb",
    colorInfo: "#2f54eb",
    colorBgLayout: "#f4f6fb",
    borderRadius: 8,
    fontSize: 14,
    wireframe: false,
  },
  components: {
    Layout: {
      headerBg: "#ffffff",
      siderBg: "#ffffff",
      bodyBg: "#f4f6fb",
    },
  },
};
