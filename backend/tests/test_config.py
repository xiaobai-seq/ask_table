import os
import unittest

from text2sql.config import Settings
from text2sql.core.llm import DashScopeLLMProvider


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

    def test_env_override_database_url(self):
        key = "TEXT2SQL_DATABASE_URL"
        original = os.environ.get(key)
        os.environ[key] = "sqlite:///./custom.db"
        try:
            self.assertEqual(Settings().database_url, "sqlite:///./custom.db")
        finally:
            # 还原环境变量，避免污染其它用例。
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original

    def test_default_dashscope_llm_model(self):
        self.assertEqual(Settings().dashscope_llm_model, "qwen3.7-plus")

    def test_dashscope_provider_defaults_to_global_model(self):
        original = os.environ.get("DASHSCOPE_LLM_MODEL")
        os.environ.pop("DASHSCOPE_LLM_MODEL", None)
        try:
            provider = DashScopeLLMProvider(api_key="test-key")
            self.assertEqual(provider.model, "qwen3.7-plus")
        finally:
            if original is None:
                os.environ.pop("DASHSCOPE_LLM_MODEL", None)
            else:
                os.environ["DASHSCOPE_LLM_MODEL"] = original


if __name__ == "__main__":
    unittest.main()
