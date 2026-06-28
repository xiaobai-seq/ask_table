import tempfile
import unittest
from pathlib import Path

from text2sql.core.models import ColumnInfo, TableInfo
from text2sql.core.retrieval import HybridTableRetriever, schema_fingerprint


class RetrievalTests(unittest.TestCase):
    def test_hybrid_retrieval_finds_order_table_among_hundreds(self):
        tables = [
            TableInfo(
                f"noise_table_{index}",
                "无关日志",
                columns=(ColumnInfo("id", "INTEGER"), ColumnInfo("payload", "TEXT"),),
            )
            for index in range(180)
        ]
        tables.append(
            TableInfo(
                "orders",
                "订单 交易 销售 金额",
                columns=(
                    ColumnInfo("order_id", "INTEGER", primary_key=True),
                    ColumnInfo("order_date", "TEXT", semantic_tags=("time",)),
                    ColumnInfo("total_amount", "REAL", semantic_tags=("metric",)),
                ),
            )
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            retriever = HybridTableRetriever(tables, cache_dir=tmpdir)
            hits = retriever.retrieve("按月份统计订单销售金额趋势", top_k=5)

        self.assertEqual(hits[0].table.name, "orders")

    def test_schema_fingerprint_changes_when_column_changes(self):
        first = [TableInfo("orders", columns=(ColumnInfo("id", "INTEGER"),))]
        second = [TableInfo("orders", columns=(ColumnInfo("id", "INTEGER"), ColumnInfo("amount", "REAL")))]

        self.assertNotEqual(schema_fingerprint(first), schema_fingerprint(second))

    def test_vector_cache_is_persisted(self):
        table = TableInfo("orders", "订单", columns=(ColumnInfo("order_id", "INTEGER"),))
        with tempfile.TemporaryDirectory() as tmpdir:
            HybridTableRetriever([table], cache_dir=tmpdir)
            self.assertTrue((Path(tmpdir) / "schema_vectors.json").exists())


if __name__ == "__main__":
    unittest.main()

