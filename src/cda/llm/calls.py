"""Single persistence entry point for model call accounting."""

from dataclasses import asdict
from decimal import Decimal
from uuid import UUID

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from cda.db.models import LLMCall
from cda.llm.types import LLMResponse


async def record_call(
    conn: AsyncConnection,
    *,
    run_id: UUID,
    check_id: str | None,
    step: int,
    response: LLMResponse,
    cost: Decimal,
) -> None:
    await conn.execute(
        insert(LLMCall).values(
            run_id=run_id,
            check_id=check_id,
            step=step,
            **asdict(response.fingerprint),
            system_fingerprint=response.system_fingerprint,
            **asdict(response.usage),
            cost_usd=cost,
            latency_ms=response.latency_ms,
        )
    )
