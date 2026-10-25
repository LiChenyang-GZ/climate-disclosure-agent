"""Immediately committed, atomic reservations shared by all run workers."""

from contextvars import ContextVar
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from cda.db.models import Run


class RunBudget:
    def __init__(self, engine: AsyncEngine, run_id: UUID) -> None:
        self.engine = engine
        self.run_id = run_id
        self._refusal: ContextVar[str | None] = ContextVar("budget_refusal", default=None)

    @property
    def refusal_reason(self) -> str | None:
        """The current worker's last refusal; concurrent workers cannot overwrite it."""
        return self._refusal.get()

    async def reserve(self, amount: Decimal) -> bool:
        async with self.engine.begin() as conn:
            result = await conn.execute(
                update(Run)
                .where(
                    Run.id == self.run_id,
                    Run.status == "running",
                    Run.spent_usd + amount <= Run.budget_usd,
                )
                .values(spent_usd=Run.spent_usd + amount)
                .returning(Run.spent_usd)
            )
            if result.scalar_one_or_none() is not None:
                self._refusal.set(None)
                return True
            status = await conn.scalar(select(Run.status).where(Run.id == self.run_id))
            self._refusal.set("budget_exhausted" if status == "running" else status)
            return False

    async def settle(self, reserved: Decimal, actual: Decimal) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                update(Run)
                .where(Run.id == self.run_id)
                .values(spent_usd=func.greatest(0, Run.spent_usd - reserved + actual))
            )
