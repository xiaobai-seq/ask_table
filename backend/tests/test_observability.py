import unittest

from text2sql.core.observability import TraceEvent, TraceRecorder


class _FakeLangfuseObservation:
    def __init__(self):
        self.ended = False

    def end(self):
        self.ended = True


class _FakeLangfuseV4:
    def __init__(self):
        self.observation = _FakeLangfuseObservation()
        self.started = []
        self.flushed = False

    def start_observation(self, **kwargs):
        self.started.append(kwargs)
        return self.observation

    def flush(self):
        self.flushed = True


class TraceRecorderLangfuseTests(unittest.TestCase):
    def test_flush_langfuse_supports_v4_observation_api(self):
        client = _FakeLangfuseV4()
        recorder = TraceRecorder()
        recorder.trace_id = "a" * 32
        recorder._langfuse = client

        recorder._flush_langfuse(
            TraceEvent(
                name="schema_inspector",
                input={"query": "sales"},
                output={"tables": ["orders"]},
                elapsed_ms=12.5,
            )
        )

        self.assertEqual(len(client.started), 1)
        payload = client.started[0]
        self.assertEqual(payload["name"], "schema_inspector")
        self.assertEqual(payload["as_type"], "span")
        self.assertEqual(payload["trace_context"], {"trace_id": "a" * 32})
        self.assertEqual(payload["input"], {"query": "sales"})
        self.assertEqual(payload["output"], {"tables": ["orders"]})
        self.assertEqual(payload["metadata"]["elapsed_ms"], 12.5)
        self.assertIsNone(payload["metadata"]["error"])
        self.assertTrue(client.observation.ended)
        self.assertTrue(client.flushed)


if __name__ == "__main__":
    unittest.main()
