import tempfile
import unittest
from pathlib import Path

from text2sql.accuracy.schema_semantics import SchemaSemantics

SAMPLE_YAML = """
tables:
  orders:
    alias: 订单
    description: 订单交易事实表，记录每笔订单的金额与状态
    columns:
      status:
        alias: 订单状态
        description: 订单当前生命周期状态
        enum_values: [paid, refunded, pending]
      total_amount:
        alias: 订单金额
        description: 订单总金额（元）
"""


class SchemaSemanticsTests(unittest.TestCase):
    def _load_sample(self) -> SchemaSemantics:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "schema_metadata.yaml"
            path.write_text(SAMPLE_YAML, encoding="utf-8")
            return SchemaSemantics.from_yaml(path)

    def test_load_yaml_exposes_table_alias_and_column_meta(self):
        semantics = self._load_sample()
        self.assertEqual(semantics.table_alias("orders"), "订单")
        self.assertEqual(semantics.column_alias("orders", "status"), "订单状态")
        self.assertEqual(
            semantics.enum_values("orders", "status"),
            ("paid", "refunded", "pending"),
        )

    def test_enrich_corpus_includes_alias_description_and_enum_words(self):
        semantics = self._load_sample()
        corpus = semantics.enrich_corpus("orders")
        self.assertIn("订单", corpus)
        self.assertIn("订单金额", corpus)
        self.assertIn("paid", corpus)

    def test_prompt_hints_emits_enum_dictionary_for_known_tables(self):
        semantics = self._load_sample()
        hints = semantics.prompt_hints(["orders"])
        self.assertIn("orders.status", hints)
        self.assertIn("paid", hints)
        # 未在元数据中的表不应产生提示
        self.assertNotIn("unknown_table", semantics.prompt_hints(["unknown_table"]))

    def test_missing_file_degrades_to_empty_without_error(self):
        semantics = SchemaSemantics.from_yaml("/nonexistent/path/schema_metadata.yaml")
        self.assertIsNone(semantics.table_alias("orders"))
        self.assertEqual(semantics.enum_values("orders", "status"), ())
        self.assertEqual(semantics.enrich_corpus("orders"), "")
        self.assertEqual(semantics.prompt_hints(["orders"]), "")


if __name__ == "__main__":
    unittest.main()
