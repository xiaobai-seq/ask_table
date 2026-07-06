import tempfile
import unittest
from pathlib import Path

from text2sql.config.domain_profile import DomainProfile
from text2sql.core.models import ColumnInfo, ForeignKeyInfo, TableInfo
from text2sql.core.retrieval import HybridTableRetriever, schema_fingerprint


class _NoVectorEmbedding:
    def embed(self, text: str):
        return []

    def batch_embed(self, texts):
        return [[] for _ in texts]


class _FlatEmbedding:
    def embed(self, text: str):
        return [1.0]

    def batch_embed(self, texts):
        return [[1.0] for _ in texts]


def _example_domain_profile() -> DomainProfile:
    return DomainProfile.from_yaml(Path(__file__).resolve().parents[1] / "examples/domain_profile.yaml")


class RetrievalTests(unittest.TestCase):
    def test_hybrid_retrieval_finds_order_table_among_hundreds(self):
        tables = [
            TableInfo(
                f"noise_table_{index}",
                "无关日志",
                columns=(ColumnInfo("id", "INTEGER"), ColumnInfo("payload", "TEXT"),),
            )
            for index in range(180)
        ]
        tables.append(
            TableInfo(
                "orders",
                "订单 交易 销售 金额",
                columns=(
                    ColumnInfo("order_id", "INTEGER", primary_key=True),
                    ColumnInfo("order_date", "TEXT", semantic_tags=("time",)),
                    ColumnInfo("total_amount", "REAL", semantic_tags=("metric",)),
                ),
            )
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            retriever = HybridTableRetriever(tables, cache_dir=tmpdir)
            hits = retriever.retrieve("按月份统计订单销售金额趋势", top_k=5)

        self.assertEqual(hits[0].table.name, "orders")

    def test_schema_fingerprint_changes_when_column_changes(self):
        first = [TableInfo("orders", columns=(ColumnInfo("id", "INTEGER"),))]
        second = [TableInfo("orders", columns=(ColumnInfo("id", "INTEGER"), ColumnInfo("amount", "REAL")))]

        self.assertNotEqual(schema_fingerprint(first), schema_fingerprint(second))

    def test_vector_cache_is_persisted(self):
        table = TableInfo("orders", "订单", columns=(ColumnInfo("order_id", "INTEGER"),))
        with tempfile.TemporaryDirectory() as tmpdir:
            HybridTableRetriever([table], cache_dir=tmpdir)
            self.assertTrue((Path(tmpdir) / "schema_vectors.json").exists())

    def test_relationship_bridge_tables_are_promoted_for_multihop_product_queries(self):
        tables = [
            TableInfo(
                "order_items",
                "订单商品明细 销售金额",
                columns=(
                    ColumnInfo("order_item_id", "INTEGER", primary_key=True),
                    ColumnInfo("sku_id", "INTEGER"),
                    ColumnInfo("subtotal", "REAL", semantic_tags=("metric",)),
                ),
                foreign_keys=(
                    ForeignKeyInfo("order_items", "sku_id", "skus", "sku_id"),
                ),
            ),
            TableInfo(
                "skus",
                "商品SKU",
                columns=(
                    ColumnInfo("sku_id", "INTEGER", primary_key=True),
                    ColumnInfo("spu_id", "INTEGER"),
                ),
                foreign_keys=(ForeignKeyInfo("skus", "spu_id", "spus", "spu_id"),),
            ),
            TableInfo(
                "spus",
                "商品SPU 品牌",
                columns=(
                    ColumnInfo("spu_id", "INTEGER", primary_key=True),
                    ColumnInfo("brand_id", "INTEGER"),
                ),
                foreign_keys=(ForeignKeyInfo("spus", "brand_id", "brands", "brand_id"),),
            ),
            TableInfo(
                "brands",
                "品牌维表",
                columns=(ColumnInfo("brand_id", "INTEGER", primary_key=True),),
            ),
            TableInfo("noise", "无关配置", columns=(ColumnInfo("id", "INTEGER"),)),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            retriever = HybridTableRetriever(
                tables,
                embedding_provider=_NoVectorEmbedding(),
                cache_dir=tmpdir,
                domain_profile=_example_domain_profile(),
            )
            hits = retriever.retrieve("各品牌销售额", top_k=4)

        self.assertEqual(
            {"order_items", "skus", "spus", "brands"},
            {hit.table.name for hit in hits},
        )
        self.assertIn(
            "relationship_path",
            next(hit for hit in hits if hit.table.name == "skus").reasons,
        )

    def test_relationship_bridge_tables_are_promoted_for_category_phrase(self):
        tables = [
            TableInfo(
                "order_items",
                "订单商品明细 销售金额",
                columns=(
                    ColumnInfo("order_item_id", "INTEGER", primary_key=True),
                    ColumnInfo("sku_id", "INTEGER"),
                    ColumnInfo("subtotal", "REAL", semantic_tags=("metric",)),
                ),
                foreign_keys=(
                    ForeignKeyInfo("order_items", "sku_id", "skus", "sku_id"),
                ),
            ),
            TableInfo(
                "skus",
                "商品SKU",
                columns=(
                    ColumnInfo("sku_id", "INTEGER", primary_key=True),
                    ColumnInfo("spu_id", "INTEGER"),
                ),
                foreign_keys=(ForeignKeyInfo("skus", "spu_id", "spus", "spu_id"),),
            ),
            TableInfo(
                "spus",
                "商品SPU 类目",
                columns=(
                    ColumnInfo("spu_id", "INTEGER", primary_key=True),
                    ColumnInfo("category_id", "INTEGER"),
                ),
                foreign_keys=(ForeignKeyInfo("spus", "category_id", "categories", "category_id"),),
            ),
            TableInfo(
                "categories",
                "一级类目 类目 目录 分类维表",
                columns=(ColumnInfo("category_id", "INTEGER", primary_key=True),),
            ),
            TableInfo("noise", "无关配置", columns=(ColumnInfo("id", "INTEGER"),)),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            retriever = HybridTableRetriever(
                tables,
                embedding_provider=_NoVectorEmbedding(),
                cache_dir=tmpdir,
                domain_profile=_example_domain_profile(),
            )
            hits = retriever.retrieve("各一级类目的销售额", top_k=4)

        self.assertEqual(
            {"order_items", "skus", "spus", "categories"},
            {hit.table.name for hit in hits},
        )
        self.assertIn(
            "relationship_path",
            next(hit for hit in hits if hit.table.name == "skus").reasons,
        )

    def test_never_reviewed_sku_query_prioritizes_skus_and_reviews(self):
        tables = [
            TableInfo(
                "product_reviews",
                "商品评价 评分",
                columns=(
                    ColumnInfo("review_id", "INTEGER", primary_key=True),
                    ColumnInfo("sku_id", "INTEGER"),
                ),
            ),
            TableInfo(
                "order_items",
                "订单商品明细 销售金额",
                columns=(ColumnInfo("sku_id", "INTEGER"),),
            ),
            TableInfo(
                "return_items",
                "退货商品明细",
                columns=(ColumnInfo("sku_id", "INTEGER"),),
            ),
            TableInfo(
                "skus",
                "商品SKU",
                columns=(ColumnInfo("sku_id", "INTEGER", primary_key=True),),
            ),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            retriever = HybridTableRetriever(
                tables,
                embedding_provider=_NoVectorEmbedding(),
                cache_dir=tmpdir,
                domain_profile=_example_domain_profile(),
            )
            hits = retriever.retrieve("从未被评价过的SKU数量", top_k=4)

        self.assertEqual(
            {"skus", "product_reviews"},
            {hit.table.name for hit in hits[:2]},
        )

    def test_table_boost_rules_are_loaded_from_domain_profile(self):
        profile = DomainProfile(
            {
                "name": "support",
                "retrieval": {
                    "table_boost_rules": [
                        {
                            "match": {"terms_any": ["工单"]},
                            "boosts": [{"tables": ["tickets"], "boost": 1.0}],
                        }
                    ]
                },
            }
        )
        tables = [
            TableInfo("generic_logs", "generic logs", columns=(ColumnInfo("id", "INTEGER"),)),
            TableInfo("tickets", "support request records", columns=(ColumnInfo("id", "INTEGER"),)),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            retriever = HybridTableRetriever(
                tables,
                embedding_provider=_FlatEmbedding(),
                cache_dir=tmpdir,
                domain_profile=profile,
            )
            hits = retriever.retrieve("工单数量", top_k=2)

        self.assertEqual(hits[0].table.name, "tickets")


if __name__ == "__main__":
    unittest.main()
