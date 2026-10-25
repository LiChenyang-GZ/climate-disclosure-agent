"""Code-owned minimum search requirement."""

from cda.tools.context import Coverage


def accept(coverage: Coverage, min_searches: int, all_part_ids: set[str]) -> bool:
    return coverage.distinct_queries >= min_searches and not coverage.missing_parts(all_part_ids)
