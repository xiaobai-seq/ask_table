import tempfile
import unittest
from pathlib import Path

from text2sql.accuracy.schema_semantics import SchemaSemantics
from text2sql.core.models import ColumnInfo, TableInfo
from text2sql.core.retrieval import HybridTableRetriever

# 说明（基于事实）：分词器内置了一份中英业务同义词表（如 订单→orders、金额→amount），
# 对这些词即使不注入语义也能跨语言召回。为真实体现「schema 语义注入」的独立增益，
# 这里特意选用同义词表之外的中文别名（库存/inventory），使改进只能归因于语义元数据。
SEMANTICS_YAML = """
tables:
  inventory:
    alias: 库存
    description: 库存水位与仓储分布快照
    columns:
      stock_level:
        alias: 库存水位
"""


class _NoVectorEmbedding:
    """空向量 provider：关闭 hashing 向量通道，隔离出 BM25 词法召回通道。

    默认 hashing 向量会给所有表一个与内容弱相关的分数，引入与语义无关的排名噪声；
    本测试只验证「语义增强语料 → 词法召回」这条链路，故停用向量通道以保证确定性。
    """

    def embed(self, text: str):
        return []

    def batch_embed(self, texts):
        return [[] for _ in texts]


def _build_tables() -> list[TableInfo]:
    cols = (ColumnInfo("id", "INTEGER"), ColumnInfo("value", "TEXT"))
    noise = [TableInfo(f"tbl_{i}", "generic data table", columns=cols) for i in range(20)]
    # 目标表注释为英文，单看 schema 无法被中文「库存」查询命中。
    target = TableInfo("inventory", "warehouse snapshot stock", columns=cols)
    return [*noise, target]


class RetrievalSemanticsIntegrationTest(unittest.TestCase):
    def _retrieve_names(self, semantics: SchemaSemantics | None) -> list[str]:
        tables = _build_tables()
        with tempfile.TemporaryDirectory() as tmpdir:
            retriever = HybridTableRetriever(
                tables,
                embedding_provider=_NoVectorEmbedding(),
                cache_dir=tmpdir,
                semantics=semantics,
            )
            hits = retriever.retrieve("库存水位分布", top_k=len(tables))
        return [hit.table.name for hit in hits]

    def test_semantics_injection_improves_recall_ranking(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "schema_metadata.yaml"
            path.write_text(SEMANTICS_YAML, encoding="utf-8")
            semantics = SchemaSemantics.from_yaml(path)

            without = self._retrieve_names(None)
            with_semantics = self._retrieve_names(semantics)

        # 未注入语义：中文查询触达不到英文 schema，目标表压根召不回。
        self.assertNotIn("inventory", without)
        # 注入语义后：目标表被召回且跃居第一，排名显著提升。
        self.assertEqual(with_semantics[0], "inventory")


if __name__ == "__main__":
    unittest.main()
