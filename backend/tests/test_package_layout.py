import unittest


class PackageLayoutTest(unittest.TestCase):
    def test_core_exports(self):
        from text2sql.core import AgentState, Text2SQLWorkflow

        self.assertIsNotNone(Text2SQLWorkflow)
        self.assertIsNotNone(AgentState)


if __name__ == "__main__":
    unittest.main()
