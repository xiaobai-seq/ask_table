# Enterprise Text2SQL Backend

企业级 Text2SQL 自然语言数据问答系统的后端包（`text2sql`）。

完整设计、链路说明与 API 文档见仓库根目录的 [`README.md`](../README.md)。

## 快速开始

以下命令均在本目录（`backend/`）下执行：

```bash
PYTHONPATH=src python3 -m text2sql.core.sample_data --output examples/demo.db
PYTHONPATH=src python3 -m unittest discover -s tests
```

安装依赖并启动 API（推荐在仓库根目录执行）：

```bash
../scripts/start-backend.sh
```

脚本默认读取根目录 `.env`，必要时创建/复用 `backend/.venv` 安装依赖，并启动 `http://127.0.0.1:8000`；可通过 `TEXT2SQL_API_PORT=8001`、`TEXT2SQL_API_RELOAD=0` 等环境变量覆盖。

也可以手动启动：

```bash
python3 -m pip install -e .
python3 -m text2sql.core.sample_data --output examples/demo.db
uvicorn text2sql.api:app --reload
```
