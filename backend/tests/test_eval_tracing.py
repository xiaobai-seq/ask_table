import unittest

from text2sql.core.models import EvalResult, to_plain


class EvalResultTraceTests(unittest.TestCase):
    """EvalResult 需携带逐 case trace，并可被 to_plain 序列化进报告。"""

    def test_trace_defaults_to_none_for_backward_compat(self):
        # 不传 trace 时保持既有位置参数签名不变。
        result = EvalResult("c1", True, {"table_recall": 1.0}, "SELECT 1")
        self.assertIsNone(result.trace)

    def test_trace_is_serialized_by_to_plain(self):
        result = EvalResult(
            "c1",
            True,
            {"table_recall": 1.0},
            "SELECT 1",
            trace={
                "prompt": "PROMPT_X",
                "retrieval_hits": [{"table": "orders", "score": 0.9}],
            },
        )
        plain = to_plain(result)
        self.assertEqual(plain["trace"]["prompt"], "PROMPT_X")
        self.assertEqual(plain["trace"]["retrieval_hits"][0]["table"], "orders")


if __name__ == "__main__":
    unittest.main()
