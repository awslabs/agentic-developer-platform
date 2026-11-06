"""Actual human artifacts, bounded selections and explicit semantic review."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Id = Annotated[str, Field(pattern=r"^[A-Za-z0-9_:/.-]{1,255}$")]
Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Prepare(Strict):
    source_key: Hash
    flow_id: Id
    challenge_id: Id
    prompt: str = Field(min_length=8, max_length=8000)
    expected_behavior: str = Field(min_length=8, max_length=1000)
    source_excerpt: str = Field(min_length=8, max_length=4000)
    reason: str = Field(min_length=8, max_length=1000)

    @model_validator(mode="after")
    def no_answer_in_prompt(self):
        if self.expected_behavior.casefold() in self.prompt.casefold() or self.source_excerpt.casefold() in self.prompt.casefold():
            raise ValueError("The native prompt cannot supply the predeclared expected behavior or source excerpt")
        return self


class AssetReadback(Strict):
    asset_id: Id
    version: int = Field(gt=0)
    observed_at: AwareDatetime = Field(strict=False)
    content: str = Field(min_length=1, max_length=1048576)
    metadata: dict


class Turn(Strict):
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=16000)


class Observe(Strict):
    preparation_hash: Hash
    native_session_id: Id
    native_agent_type: Id
    session_started_at: AwareDatetime = Field(strict=False)
    before: AssetReadback
    after: AssetReadback
    transcript: list[Turn] = Field(min_length=2, max_length=32)
    response_quote: str = Field(min_length=8, max_length=4000)
    source_citation_quote: str = Field(min_length=1, max_length=4000)
    skill_invocation_quote: str | None = Field(default=None, min_length=1, max_length=4000)
    explanation: str = Field(min_length=30, max_length=4000)


class Review(Strict):
    evidence_id: Id
    observation_hash: Hash
    source_effect_explanation: str = Field(min_length=30, max_length=4000)


class Complete(Strict):
    reviews: list[Review] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def unique(self):
        if len({review.evidence_id for review in self.reviews}) != len(self.reviews):
            raise ValueError("Each evidence record must be reviewed exactly once")
        return self
