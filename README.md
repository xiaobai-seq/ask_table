# Enterprise Text2SQL

一个面向企业自助取数场景的 Text2SQL 后端骨架，覆盖：

- LangGraph 状态驱动工作流：schema 检索、关系分析、SQL 生成、执行、总结、渲染。
- BM25 + 向量召回 + RRF + rerank 的三层混合表检索。
- schema 指纹缓存与持久化向量索引。
- Neo4j 可选图谱增强，多表 JOIN 路径解析。
- 约束化 SQL prompt、复杂查询策略、模糊问题澄清。
- SSE 流式响应、任务取消、评测 CLI 与样例数据。

本仓库默认可在缺少 DashScope、FAISS、Neo4j、SQLAlchemy、FastAPI 的本地环境中运行核心测试；安装依赖后会自动启用对应生产适配器。

## 链路速览

一次查询的主链路在 `backend/src/text2sql/core/graph.py` 的 `Text2SQLWorkflow` 中编排：

1. `schema_inspector`：用 `ConversationMemory` 改写追问，并通过 `HybridTableRetriever` 检索候选表。
2. `table_relationship`：用外键图或 Neo4j 解析候选表之间的 JOIN 路径。
3. `sql_generator`：优先走 LLM prompt，未配置或失败时走规则生成器，产出 `SQLPlan`。
4. `sql_executor`：用 `SQLValidator` 拦截非只读 SQL 和未知表字段，再执行查询。
5. `summarize`：把执行结果转成业务摘要，LLM 不可用时用本地统计兜底。
6. `data_render`：根据 SQL 计划和结果字段推荐图表类型，并把本轮结果写入会话记忆。

API 层位于 `backend/src/text2sql/api.py`，只负责把上述节点的增量状态包装成 SSE 事件。

## 快速开始

后端代码位于 `backend/`，以下命令均在该目录下执行：

```bash
cd backend
PYTHONPATH=src python3 -m text2sql.core.sample_data --output examples/demo.db
PYTHONPATH=src python3 -m unittest discover -s tests
```

安装依赖并启动 API：

```bash
cd backend
python3 -m pip install -e .
python3 -m text2sql.core.sample_data --output examples/demo.db
uvicorn text2sql.api:app --reload
```

评测：

```bash
cd backend
python3 -m text2sql.eval --db examples/demo.db --cases examples/eval_cases.jsonl --report examples/eval_report.json
```

## API

`POST /query`

```json
{
  "query": "按月份统计订单金额趋势，并计算环比增长率",
  "session_id": "demo-session"
}
```

响应为 SSE，事件会按节点持续返回：`schema_inspector`、`table_relationship`、`sql_generator`、`sql_executor`、`summarize`、`data_render`。

`POST /cancel/{task_id}` 可取消长查询。

## 设计重点

对话上下文通过 `ConversationMemory` 做 session 级窗口管理、指代补全和历史 SQL/表结构继承。

评测通过 JSONL case 管理，输出表召回、SQL 关键词、精确匹配、执行成功率等指标，适合频繁回归。

复杂 SQL 通过 prompt 规则和生成策略共同支持窗口函数、同比/环比、排名、滚动统计和递归 CTE。对于信息不足或歧义问题，系统返回澄清问题，而不是冒进生成 SQL。
