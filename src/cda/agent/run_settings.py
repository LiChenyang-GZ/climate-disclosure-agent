"""The settings a run executes with, stored on the run (D87)."""

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from cda.agent.run_graph import RunDeps


class RunSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step_cap: int = Field(ge=2)
    token_cap: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)
    tool_timeout_s: float = Field(gt=0)
    seed: int | None
    b_top_k: int = Field(ge=1)
    b_min_vector_score: float | None
    lc_scope: Literal["pack", "sustainability"]
    lc_max_prompt_tokens: int = Field(ge=1)

    @classmethod
    def from_deps(cls, deps: "RunDeps") -> "RunSettings":
        return cls(**{name: getattr(deps, name) for name in cls.model_fields})

    @classmethod
    def resolve(cls, stored: dict[str, Any] | None, deps: "RunDeps") -> "RunSettings":
        """Stored keys win over the worker's defaults; raises ValidationError if invalid."""
        return cls.model_validate({**cls.from_deps(deps).model_dump(), **(stored or {})})
