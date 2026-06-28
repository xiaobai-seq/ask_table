# Enterprise Text2SQL Backend

企业级 Text2SQL 自然语言数据问答系统的后端包（`text2sql`）。

完整设计、链路说明与 API 文档见仓库根目录的 [`README.md`](../README.md)。

## 快速开始

以下命令均在本目录（`backend/`）下执行：

```bash
PYTHONPATH=src python3 -m text2sql.core.sample_data --output examples/demo.db
PYTHONPATH=src python3 -m unittest discover -s tests
```

安装依赖并启动 API：

```bash
python3 -m pip install -e .
python3 -m text2sql.core.sample_data --output examples/demo.db
uvicorn text2sql.api:app --reload
```
