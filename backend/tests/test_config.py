import asyncio
import os
import unittest
from unittest.mock import patch

from text2sql.config import Settings
from text2sql.core.llm import DashScopeLLMProvider


class SettingsTest(unittest.TestCase):
    def test_default_sql_repair_max_retries(self):
        self.assertEqual(Settings().sql_repair_max_retries, 2)

    def test_default_schema_retrieval_top_k(self):
        self.assertEqual(Settings().schema_retrieval_top_k, 8)

    def test_env_override_schema_retrieval_top_k(self):
        key = "TEXT2SQL_SCHEMA_RETRIEVAL_TOP_K"
        original = os.environ.get(key)
        os.environ[key] = "10"
        try:
            self.assertEqual(Settings().schema_retrieval_top_k, 10)
        finally:
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original

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

    def test_env_override_dashscope_http_base_url(self):
        key = "DASHSCOPE_HTTP_BASE_URL"
        original = os.environ.get(key)
        os.environ[key] = "https://workspace.cn-beijing.maas.aliyuncs.com/api/v1"
        try:
            self.assertEqual(
                Settings().dashscope_http_base_url,
                "https://workspace.cn-beijing.maas.aliyuncs.com/api/v1",
            )
        finally:
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original

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

    def test_dashscope_provider_passes_base_address_to_generation(self):
        base_url = "https://workspace.cn-beijing.maas.aliyuncs.com/api/v1"
        provider = DashScopeLLMProvider(
            model="qwen-plus",
            api_key="test-key",
            base_http_api_url=base_url,
            request_timeout_seconds=42,
        )
        fake_response = {
            "output": {"choices": [{"message": {"content": "ok"}}]},
        }

        with patch("dashscope.Generation.call", return_value=fake_response) as call:
            self.assertEqual(provider._complete_sync("hello"), "ok")

        self.assertEqual(call.call_args.kwargs["base_address"], base_url)
        self.assertEqual(call.call_args.kwargs["timeout"], 42)

    def test_qwen37_provider_uses_multimodal_generation(self):
        base_url = "https://workspace.cn-beijing.maas.aliyuncs.com/api/v1"
        provider = DashScopeLLMProvider(
            model="qwen3.7-plus",
            api_key="test-key",
            base_http_api_url=base_url,
            request_timeout_seconds=43,
        )
        fake_response = {
            "output": {
                "choices": [
                    {"message": {"content": [{"text": "ok"}]}},
                ],
            },
        }

        with patch("dashscope.MultiModalConversation.call", return_value=fake_response) as call:
            self.assertEqual(provider._complete_sync("hello"), "ok")

        self.assertEqual(call.call_args.kwargs["base_address"], base_url)
        self.assertEqual(call.call_args.kwargs["timeout"], 43)
        self.assertEqual(
            call.call_args.kwargs["messages"][1]["content"],
            [{"text": "hello"}],
        )

    def test_dashscope_provider_complete_enforces_async_timeout(self):
        provider = DashScopeLLMProvider(
            model="qwen3.7-plus",
            api_key="test-key",
            request_timeout_seconds=0.01,
        )

        def slow_to_thread(*args, **kwargs):
            return asyncio.sleep(1, result="late")

        with patch("text2sql.core.llm.asyncio.to_thread", new=slow_to_thread):
            with self.assertRaises(asyncio.TimeoutError):
                asyncio.run(provider.complete("hello"))


if __name__ == "__main__":
    unittest.main()
