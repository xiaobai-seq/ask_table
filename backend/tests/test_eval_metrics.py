import unittest

from text2sql.eval import aggregate_metrics, compare_result_sets
from text2sql.core.models import EvalResult


class ResultComparisonTests(unittest.TestCase):
    def test_exact_match_scores_all_dimensions(self):
        expected = [{"region": "East", "amount": 100}, {"region": "North", "amount": 50}]
        actual = [{"region": "East", "amount": 100}, {"region": "North", "amount": 50}]
        metrics = compare_result_sets(expected, actual)
        self.assertEqual(metrics["row_count_match"], 1.0)
        self.assertEqual(metrics["column_set_match"], 1.0)
        self.assertEqual(metrics["value_set_exact"], 1.0)
        self.assertEqual(metrics["value_set_recall"], 1.0)

    def test_partial_value_overlap_is_measured(self):
        expected = [{"region": "East", "amount": 100}, {"region": "North", "amount": 50}]
        actual = [{"region": "East", "amount": 100}, {"region": "South", "amount": 70}]
        metrics = compare_result_sets(expected, actual)
        self.assertEqual(metrics["row_count_match"], 1.0)
        self.assertEqual(metrics["column_set_match"], 1.0)
        self.assertEqual(metrics["value_set_exact"], 0.0)
        # 两行中有一行完全一致 → 召回 0.5。
        self.assertAlmostEqual(metrics["value_set_recall"], 0.5)

    def test_row_count_and_column_mismatch_detected(self):
        expected = [{"region": "East", "amount": 100}]
        actual = [{"region": "East"}, {"region": "North"}]
        metrics = compare_result_sets(expected, actual)
        self.assertEqual(metrics["row_count_match"], 0.0)
        self.assertEqual(metrics["column_set_match"], 0.0)

    def test_empty_expected_and_actual_is_exact(self):
        metrics = compare_result_sets([], [])
        self.assertEqual(metrics["value_set_exact"], 1.0)
        self.assertEqual(metrics["value_set_recall"], 1.0)

    def test_aggregate_metrics_averages_across_results(self):
        results = [
            EvalResult("a", True, {"table_recall": 1.0, "value_set_recall": 1.0}, "sql"),
            EvalResult("b", False, {"table_recall": 0.0, "value_set_recall": 0.5}, "sql"),
        ]
        aggregated = aggregate_metrics(results)
        self.assertAlmostEqual(aggregated["table_recall"], 0.5)
        self.assertAlmostEqual(aggregated["value_set_recall"], 0.75)


if __name__ == "__main__":
    unittest.main()
