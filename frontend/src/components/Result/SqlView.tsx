import { Fragment } from "react";
import { Empty, Typography } from "antd";

// 常见 SQL 关键字（用于只读高亮，大小写不敏感）。
const KEYWORDS = [
  "SELECT", "FROM", "WHERE", "GROUP BY", "ORDER BY", "HAVING", "LIMIT", "OFFSET",
  "LEFT JOIN", "RIGHT JOIN", "INNER JOIN", "JOIN", "ON", "AS", "AND", "OR", "NOT",
  "IN", "IS", "NULL", "LIKE", "BETWEEN", "DISTINCT", "COUNT", "SUM", "AVG", "MAX",
  "MIN", "CASE", "WHEN", "THEN", "ELSE", "END", "ASC", "DESC", "UNION", "ALL", "BY",
];

// 多词关键字优先，避免 "GROUP BY" 被拆成两个单词。
const PATTERN = new RegExp(
  `\\b(${KEYWORDS.sort((a, b) => b.length - a.length).join("|")})\\b`,
  "gi",
);

// 把 SQL 文本切成「关键字 / 普通文本」片段，关键字着色。
function highlight(sql: string) {
  const parts = sql.split(PATTERN);
  return parts.map((part, i) => {
    if (KEYWORDS.includes(part.toUpperCase())) {
      return (
        <span key={i} style={{ color: "#2f54eb", fontWeight: 600 }}>
          {part}
        </span>
      );
    }
    return <Fragment key={i}>{part}</Fragment>;
  });
}

interface SqlViewProps {
  sql: string | null | undefined;
}

// 只读 SQL 视图：等宽字体 + 关键字高亮 + 一键复制。
export default function SqlView({ sql }: SqlViewProps) {
  if (!sql) {
    return <Empty description="本轮未生成 SQL" image={Empty.PRESENTED_IMAGE_SIMPLE} />;
  }
  return (
    <div>
      <div style={{ textAlign: "right", marginBottom: 8 }}>
        <Typography.Text copyable={{ text: sql }} type="secondary">
          复制 SQL
        </Typography.Text>
      </div>
      <pre
        style={{
          margin: 0,
          padding: 16,
          background: "#f7f8fa",
          borderRadius: 8,
          border: "1px solid #eef0f4",
          fontFamily: "'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace",
          fontSize: 13,
          lineHeight: 1.6,
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
        }}
      >
        <code>{highlight(sql)}</code>
      </pre>
    </div>
  );
}
