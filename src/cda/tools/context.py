"""Per-check context and query coverage."""

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from cda.checks.models import normalise_query
from cda.indexing.embed import Embedder


@dataclass
class Coverage:
    queries: set[tuple[str, str, frozenset[str]]] = field(default_factory=set)

    def record(self, tool: str, query: str, scope: set[str]) -> bool:
        key = (tool, normalise_query(query), frozenset(scope))
        if key in self.queries:
            return False
        self.queries.add(key)
        return True

    @property
    def distinct_queries(self) -> int:
        return len({q for _, q, _ in self.queries})

    @property
    def parts_covered(self) -> set[str]:
        return {p for _, _, scope in self.queries for p in scope}

    def missing_parts(self, all_part_ids: set[str]) -> set[str]:
        return all_part_ids - self.parts_covered


@dataclass
class ToolContext:
    conn: AsyncConnection
    user_id: UUID
    pack_id: UUID
    embedder: Embedder | None = None
    timeout_s: float = 10.0
    coverage: Coverage = field(default_factory=Coverage)
