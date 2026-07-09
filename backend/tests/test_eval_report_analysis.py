import unittest

from text2sql.eval_report import (
    analyze_report,
    load_report_from_repository,
    render_markdown,
    report_payload_from_eval_run,
)
from text2sql.persistence.repository import EvalCaseResultRecord, InMemoryEvalRunRepository


class EvalReportAnalysisTests(unittest.TestCase):
    def test_analyze_report_replays_quality_gate_for_old_report(self):
        payload = {
            "summary": {
                "total": 2,
                "passed": 1,
                "pass_rate": 0.5,
                "metrics": {"table_recall": 1.0, "value_set_exact": 0.5},
            },
            "results": [
                {
                    "case_id": "bad_paid",
                    "passed": False,
                    "metrics": {"value_set_exact": 0.0},
                    "generated_sql": "SELECT COUNT(*) FROM orders WHERE pay_status = 'paid'",
                    "errors": ["Result set mismatch"],
                    "trace": {
                        "query": "统计已支付订单数",
                        "retrieval_hits": [{"table": "orders", "score": 1.0}],
                    },
                },
                {
                    "case_id": "ok",
                    "passed": True,
                    "metrics": {"value_set_exact": 1.0},
                    "generated_sql": (
                        "SELECT COUNT(*) AS paid_order_count FROM orders "
                        "WHERE pay_status IN ('paid', 'partial_refund')"
                    ),
                    "errors": [],
                    "trace": {
                        "query": "统计已支付订单数",
                        "retrieval_hits": [{"table": "orders", "score": 1.0}],
                    },
                },
            ],
        }

        analysis = analyze_report(payload)

        self.assertEqual(analysis["failed"], 1)
        self.assertEqual(analysis["quality_gate"]["offline_failed_flagged"], 1)
        self.assertEqual(analysis["quality_gate"]["offline_passed_flagged"], 0)
        self.assertIn("partial_refund", analysis["failures"][0]["offline_quality_issues"][0])

    def test_analyze_report_reads_trace_quality_gate_fields(self):
        payload = {
            "summary": {"total": 1, "passed": 0, "pass_rate": 0.0, "metrics": {}},
            "results": [
                {
                    "case_id": "c1",
                    "passed": False,
                    "metrics": {},
                    "generated_sql": "SELECT 1",
                    "errors": ["Result set mismatch"],
                    "trace": {
                        "query": "q",
                        "quality_gate_issues": ["issue A"],
                        "quality_gate_remaining_issues": ["issue B"],
                        "quality_gate_repaired": True,
                        "quality_gate_repair_unresolved": True,
                        "quality_gate_template_fallback": True,
                    },
                }
            ],
        }

        analysis = analyze_report(payload)
        markdown = render_markdown(analysis)

        self.assertEqual(analysis["quality_gate"]["trace_issue_cases"], 1)
        self.assertEqual(analysis["quality_gate"]["repaired_cases"], 1)
        self.assertEqual(analysis["quality_gate"]["repair_unresolved_cases"], 1)
        self.assertEqual(analysis["quality_gate"]["template_fallback_cases"], 1)
        self.assertEqual(analysis["quality_gate"]["remaining_issue_cases"], 1)
        self.assertIn("issue A", markdown)
        self.assertIn("template_fallback_cases", markdown)
        self.assertIn("offline_failed_flagged", markdown)

    def test_analyze_report_replays_with_metadata_sql_dialect(self):
        payload = {
            "metadata": {"db": "mysql+pymysql://user:pw@127.0.0.1:3308/demo"},
            "summary": {"total": 1, "passed": 0, "pass_rate": 0.0, "metrics": {}},
            "results": [
                {
                    "case_id": "dialect",
                    "passed": False,
                    "metrics": {},
                    "generated_sql": (
                        "SELECT AVG(julianday(delivered_at) - julianday(shipped_at)) "
                        "FROM shipments"
                    ),
                    "errors": ["Execution failed"],
                    "trace": {
                        "query": "统计平均配送时长",
                        "retrieval_hits": [{"table": "shipments", "score": 1.0}],
                    },
                }
            ],
        }

        analysis = analyze_report(payload)
        markdown = render_markdown(analysis)

        self.assertEqual(analysis["sql_dialect"], "mysql")
        self.assertIn("方言错误", analysis["failures"][0]["offline_quality_issues"][0])
        self.assertIn("sql_dialect", markdown)

    def test_report_payload_from_eval_run_restores_case_trace(self):
        repo = InMemoryEvalRunRepository()
        run = repo.record_run(
            total=1,
            passed=0,
            pass_rate=0.0,
            metrics={"value_set_exact": 0.0},
        )
        repo.record_case_result(
            EvalCaseResultRecord(
                run_id=run.id,
                case_id="db_case",
                query="统计已支付订单数",
                passed=False,
                retrieval_hits=[{"table": "orders", "score": 1.0}],
                generated_sql="SELECT COUNT(*) FROM orders WHERE pay_status = 'paid'",
                metrics={
                    "value_set_exact": 0.0,
                    "sql_generation": {
                        "quality_gate_issues": ["issue A"],
                        "quality_gate_remaining_issues": ["issue B"],
                        "quality_gate_repaired": True,
                        "quality_gate_repair_unresolved": True,
                        "quality_gate_template_fallback": True,
                    },
                },
                errors=["Result set mismatch"],
            )
        )

        payload = load_report_from_repository(
            repo,
            run_id=run.id,
            metadata={"db": "mysql+pymysql://user:pw@127.0.0.1:3308/demo"},
        )
        analysis = analyze_report(payload)

        self.assertEqual(payload["metadata"]["source"], "repository")
        self.assertEqual(payload["metadata"]["eval_run_id"], run.id)
        self.assertEqual(payload["results"][0]["trace"]["query"], "统计已支付订单数")
        self.assertEqual(payload["results"][0]["trace"]["quality_gate_issues"], ["issue A"])
        self.assertEqual(analysis["sql_dialect"], "mysql")
        self.assertEqual(analysis["quality_gate"]["trace_issue_cases"], 1)
        self.assertEqual(analysis["quality_gate"]["repaired_cases"], 1)
        self.assertEqual(analysis["quality_gate"]["repair_unresolved_cases"], 1)
        self.assertEqual(analysis["quality_gate"]["template_fallback_cases"], 1)

    def test_report_payload_from_eval_run_uses_latest_run_by_default(self):
        repo = InMemoryEvalRunRepository()
        old = repo.record_run(total=1, passed=1, pass_rate=1.0, metrics={"old": 1})
        new = repo.record_run(total=2, passed=1, pass_rate=0.5, metrics={"new": 1})
        repo.record_case_result(EvalCaseResultRecord(run_id=old.id, case_id="old", query="old"))
        repo.record_case_result(EvalCaseResultRecord(run_id=new.id, case_id="new", query="new"))

        payload = load_report_from_repository(repo)

        self.assertEqual(payload["metadata"]["eval_run_id"], new.id)
        self.assertEqual(payload["summary"]["total"], 2)
        self.assertEqual([result["case_id"] for result in payload["results"]], ["new"])

    def test_report_payload_from_eval_run_is_json_serializable(self):
        repo = InMemoryEvalRunRepository()
        run = repo.record_run(total=1, passed=1, pass_rate=1.0, metrics={})
        repo.record_case_result(
            EvalCaseResultRecord(
                run_id=run.id,
                case_id="serializable",
                query="q",
                metrics={"sql_generation": {"quality_gate_repaired": False}},
            )
        )

        payload = report_payload_from_eval_run(run, repo.list_case_results(run.id))

        self.assertIsInstance(payload["metadata"]["run_at"], str)
        self.assertIn("quality_gate_repaired", payload["results"][0]["trace"])


if __name__ == "__main__":
    unittest.main()
