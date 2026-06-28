"""准确率工程子包。

集中放置提升 Text2SQL 生成准确率的能力：schema 语义增强、few-shot 示例库、
SQL 自修复等。这些能力对 core 工作流是可选增强，缺失时主链路仍能降级运行。
"""

from text2sql.accuracy.few_shot import (
    FewShotExample,
    FewShotStore,
    InMemoryFewShotStore,
    format_examples_block,
)
from text2sql.accuracy.schema_semantics import SchemaSemantics

__all__ = [
    "FewShotExample",
    "FewShotStore",
    "InMemoryFewShotStore",
    "SchemaSemantics",
    "format_examples_block",
]
