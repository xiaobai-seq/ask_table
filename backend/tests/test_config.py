import os
import unittest

from text2sql.config import Settings


class SettingsTest(unittest.TestCase):
    def test_default_sql_repair_max_retries(self):
        self.assertEqual(Settings().sql_repair_max_retries, 2)

    def test_env_override_sql_repair_max_retries(self):
        key = "TEXT2SQL_SQL_REPAIR_MAX_RETRIES"
        original = os.environ.get(key)
        os.environ[key] = "3"
        try:
            self.assertEqual(Settings().sql_repair_max_retries, 3)
        finally:
            # 还原环境变量，避免污染其它用例。
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original


if __name__ == "__main__":
    unittest.main()
