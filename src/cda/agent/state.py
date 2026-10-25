"""Per-check state; never shared across workers."""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from cda.agent.finding import FactArgs, SubmitFinding
from cda.llm.types import Message, ToolCall
from cda.tools.context import ToolContext
from cda.tools.envelope import ToolResult
from cda.verify.evidence import EvidenceCheck


@dataclass
class WorkerResult:
    check_id: str
    status: str
    confidence: Literal["high", "low"] | None
    rationale: str
    missing_aspects: list[str]
    relief_applied: bool
    needs_review_reason: str | None
    evidence: list[EvidenceCheck]
    facts: list[FactArgs]
    steps: int
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal


@dataclass
class WorkerState:
    context: ToolContext
    history: list[Message] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    parts: dict[str, str] = field(default_factory=dict)
    candidate_units: list[str] = field(default_factory=list)
    step: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost: Decimal = Decimal(0)
    forced: bool = False
    invalid: int = 0
    verification_failures: int = 0
    call: ToolCall | None = None
    tool_result: ToolResult | None = None
    submission: SubmitFinding | None = None
    evidence: list[EvidenceCheck] = field(default_factory=list)
    result: WorkerResult | None = None
    route: str = "build_prompt"
