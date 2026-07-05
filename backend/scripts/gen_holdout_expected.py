#!/usr/bin/env python3
"""对 holdout 用例执行 expected_sql，回填 expected_result 作为 ground truth。

只读 ecommerce.db，按每条 expected_sql 取全部行写回 eval_cases_holdout.jsonl，
并打印每个 case 的行数与前若干行，供人工审查 SQL 是否符合业务预期。
数据由固定随机种子生成，结果可复现。
"""

import json
import sqlite3
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = _BACKEND_ROOT / "examples" / "ecommerce" / "ecommerce.db"
CASES_PATH = _BACKEND_ROOT / "examples" / "ecommerce" / "eval_cases_holdout.jsonl"

_PREVIEW_ROWS = 3


def _load_cases() -> list[dict]:
    """读取 holdout 用例（每行一个 JSON 对象）。"""
    lines = CASES_PATH.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _run_sql(conn: sqlite3.Connection, sql: str) -> list[dict]:
    """执行只读查询并返回列名->值的行列表。"""
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(sql).fetchall()]


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        cases = _load_cases()
        for case in cases:
            rows = _run_sql(conn, case["expected_sql"])
            case["expected_result"] = rows
            preview = json.dumps(rows[:_PREVIEW_ROWS], ensure_ascii=False)
            print(f"[{case['case_id']}] rows={len(rows)} preview={preview}")
    finally:
        conn.close()

    payload = "\n".join(json.dumps(case, ensure_ascii=False) for case in cases) + "\n"
    CASES_PATH.write_text(payload, encoding="utf-8")
    print(f"\nOK: {len(cases)} cases 回填 expected_result -> {CASES_PATH}")


if __name__ == "__main__":
    main()
