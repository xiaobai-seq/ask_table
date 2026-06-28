from __future__ import annotations

"""表关系解析。

schema 检索只知道“可能相关的表”，SQL 生成还需要知道这些表能否 JOIN。
本模块把外键建成图，或接入 Neo4j 图谱，输出候选表之间的最短关系路径。
"""

import os
from collections import deque
from itertools import combinations

from text2sql.core.models import ForeignKeyInfo, RelationshipPath, TableInfo


class RelationshipResolver:
    """关系解析器接口，方便本地图和 Neo4j 图谱互换。"""

    def paths_for_tables(self, tables: list[TableInfo], max_depth: int = 4) -> list[RelationshipPath]:
        raise NotImplementedError


class InMemoryRelationshipResolver(RelationshipResolver):
    """基于 schema 外键的内存 BFS 关系解析器。"""

    def __init__(self, all_tables: list[TableInfo]) -> None:
        self.all_tables = {table.name: table for table in all_tables}
        self.adjacency: dict[str, list[tuple[str, ForeignKeyInfo]]] = {}
        for table in all_tables:
            self.adjacency.setdefault(table.name, [])
            for fk in table.foreign_keys:
                # 外键原方向和反方向都入图，方便从事实表或维表任一侧开始搜索。
                self.adjacency.setdefault(fk.source_table, []).append((fk.target_table, fk))
                reverse = ForeignKeyInfo(
                    source_table=fk.target_table,
                    source_column=fk.target_column,
                    target_table=fk.source_table,
                    target_column=fk.source_column,
                )
                self.adjacency.setdefault(fk.target_table, []).append((fk.source_table, reverse))

    def paths_for_tables(self, tables: list[TableInfo], max_depth: int = 4) -> list[RelationshipPath]:
        # 对召回候选表两两找最短路径，SQL 生成器再按需要挑可用路径。
        table_names = [table.name for table in tables]
        paths: list[RelationshipPath] = []
        for source, target in combinations(table_names, 2):
            path = self._shortest_path(source, target, max_depth)
            if path is not None:
                paths.append(path)
        return paths

    def _shortest_path(self, source: str, target: str, max_depth: int) -> RelationshipPath | None:
        # BFS 能优先返回最少 JOIN 数的路径，通常也是最稳妥的 SQL 连接方式。
        if source == target:
            return RelationshipPath(source, target, ())
        queue: deque[tuple[str, list[ForeignKeyInfo]]] = deque([(source, [])])
        visited = {source}
        while queue:
            current, joins = queue.popleft()
            if len(joins) >= max_depth:
                continue
            for neighbor, fk in self.adjacency.get(current, []):
                if neighbor in visited:
                    continue
                next_joins = joins + [fk]
                if neighbor == target:
                    return RelationshipPath(source, target, tuple(next_joins))
                visited.add(neighbor)
                queue.append((neighbor, next_joins))
        return None


class Neo4jRelationshipResolver(RelationshipResolver):
    """可选的企业知识图谱关系解析器。"""

    def __init__(self, uri: str | None = None, user: str | None = None, password: str | None = None) -> None:
        self.uri = uri or os.getenv("NEO4J_URI", "bolt://localhost:7687")
        self.user = user or os.getenv("NEO4J_USER", "neo4j")
        self.password = password or os.getenv("NEO4J_PASSWORD", "")
        try:  # pragma: no cover - optional service dependency
            from neo4j import GraphDatabase
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("neo4j package is not installed") from exc
        self.driver = GraphDatabase.driver(self.uri, auth=(self.user, self.password))

    def paths_for_tables(self, tables: list[TableInfo], max_depth: int = 4) -> list[RelationshipPath]:
        names = [table.name for table in tables]
        if len(names) < 2:
            return []
        # 图谱中可提前沉淀人工确认过的 FK_TO 边，适合跨库或弱外键场景。
        query = """
        MATCH (a:Table)
        WHERE a.name IN $names
        MATCH (b:Table)
        WHERE b.name IN $names AND a.name < b.name
        MATCH p = shortestPath((a)-[:FK_TO*..4]-(b))
        RETURN a.name AS source, b.name AS target,
               [r IN relationships(p) | {
                 source_table: startNode(r).name,
                 source_column: r.source_column,
                 target_table: endNode(r).name,
                 target_column: r.target_column
               }] AS joins
        """
        paths: list[RelationshipPath] = []
        with self.driver.session() as session:  # pragma: no cover
            for record in session.run(query, names=names, max_depth=max_depth):
                joins = tuple(
                    ForeignKeyInfo(
                        item["source_table"],
                        item["source_column"],
                        item["target_table"],
                        item["target_column"],
                    )
                    for item in record["joins"]
                )
                paths.append(RelationshipPath(record["source"], record["target"], joins))
        return paths


def default_relationship_resolver(tables: list[TableInfo]) -> RelationshipResolver:
    """有 Neo4j 配置时优先图谱，否则回退到 schema 外键图。"""

    if os.getenv("NEO4J_URI") and os.getenv("NEO4J_PASSWORD"):
        try:  # pragma: no cover - optional service dependency
            return Neo4jRelationshipResolver()
        except Exception:
            pass
    return InMemoryRelationshipResolver(tables)
