import tempfile
import unittest
from pathlib import Path

from text2sql.config.domain_profile import DomainProfile, set_active_domain_profile
from text2sql.core.models import ColumnInfo, RetrievalHit, TableInfo
from text2sql.core.schema import infer_column_tags, infer_table_comment
from text2sql.core.sql_generator import DeterministicSQLGenerator
from text2sql.core.tokenization import tokenize


CUSTOM_PROFILE = """
name: support
synonyms:
  工单: [ticket, tickets, issue]
schema:
  table_comment_rules:
    - contains_any: [ticket]
      comment: 工单 服务 支持
  table_tag_keywords: [ticket]
  column_tag_keywords:
    time: [opened]
    metric: [duration]
    dimension: [severity]
sql:
  column_hints:
    time: [opened]
    metric: [duration]
    dimension: [severity]
  intent_terms:
    growth: [走势]
    time_metric: [走势]
    kpi: [总时长]
clarification:
  options: [工单处理时长, 工单数量]
frontend:
  example_queries: [按月份统计工单处理时长走势]
"""


class DomainProfileTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_active_domain_profile(DomainProfile.default())

    def _load_custom_profile(self) -> DomainProfile:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "domain_profile.yaml"
            path.write_text(CUSTOM_PROFILE, encoding="utf-8")
            return DomainProfile.from_yaml(path)

    def test_yaml_profile_overrides_default_terms(self):
        profile = self._load_custom_profile()

        self.assertEqual(profile.name, "support")
        self.assertIn("ticket", profile.synonyms["工单"])
        self.assertEqual(profile.frontend_examples, ("按月份统计工单处理时长走势",))

    def test_active_profile_drives_tokenization_and_schema_inference(self):
        profile = self._load_custom_profile()
        set_active_domain_profile(profile)

        self.assertIn("ticket", tokenize("工单走势"))
        self.assertEqual(infer_table_comment("tickets"), "工单 服务 支持")
        self.assertIn("metric", infer_column_tags("duration_minutes", "REAL"))

    def test_sql_generator_uses_profile_intents_for_non_order_domain(self):
        profile = self._load_custom_profile()
        table = TableInfo(
            "tickets",
            "support tickets",
            columns=(
                ColumnInfo("ticket_id", "INTEGER", primary_key=True),
                ColumnInfo("opened_month", "TEXT", semantic_tags=("time",)),
                ColumnInfo("duration_minutes", "REAL", semantic_tags=("metric",)),
                ColumnInfo("severity", "TEXT", semantic_tags=("dimension",)),
            ),
        )
        generator = DeterministicSQLGenerator(domain_profile=profile)

        plan = generator.generate(
            "按月份统计工单处理时长走势",
            [RetrievalHit(table, 1.0)],
            [],
        )

        self.assertIn("SUM(duration_minutes)", plan.sql)
        self.assertEqual(plan.chart_type, "line")


if __name__ == "__main__":
    unittest.main()
