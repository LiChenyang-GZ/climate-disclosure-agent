"""Atomic in-process budget reservations."""

import asyncio
from decimal import Decimal
from typing import Protocol


class Budget(Protocol):
    async def reserve(self, amount: Decimal) -> bool: ...
    async def settle(self, reserved: Decimal, actual: Decimal) -> None: ...


class MemoryBudget:
    def __init__(self, limit: Decimal) -> None:
        self.limit = limit
        self.spent = Decimal(0)
        self.reserved = Decimal(0)
        self._lock = asyncio.Lock()

    async def reserve(self, amount: Decimal) -> bool:
        if amount < 0:
            raise ValueError("Negative reservation")
        async with self._lock:
            if self.spent + self.reserved + amount > self.limit:
                return False
            self.reserved += amount
            return True

    async def settle(self, reserved: Decimal, actual: Decimal) -> None:
        async with self._lock:
            self.reserved -= reserved
            self.spent += actual
