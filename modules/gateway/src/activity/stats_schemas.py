"""Stats API Pydantic schemas — Issue #3630.

Response models for GET /me/agent-run-stats and GET /admin/agent-run-stats.
These endpoints aggregate DynamoDB invocation data into a single dashboard payload.
"""

from pydantic import BaseModel, Field


class TodayCounts(BaseModel):
    """Today's aggregate run counts."""

    total: int = Field(default=0, description="Total runs today (excludes no_op/webhook_received).")
    completed: int = Field(default=0, description="Runs with status=complete today.")
    failed: int = Field(default=0, description="Runs with status=failed today.")
    active: int = Field(default=0, description="Runs currently in-progress today.")


class DailyEntry(BaseModel):
    """Per-day breakdown of run counts."""

    date: str = Field(description="ISO 8601 date (YYYY-MM-DD).")
    total: int = Field(default=0, description="Total runs on this day.")
    completed: int = Field(default=0, description="Completed runs on this day.")
    failed: int = Field(default=0, description="Failed runs on this day.")


class PersonaStats(BaseModel):
    """Per-persona aggregate stats."""

    persona: str = Field(description="Persona identifier (e.g., 'developer', 'reviewer').")
    total: int = Field(default=0, description="Total runs by this persona in the window.")
    completed: int = Field(default=0, description="Completed runs by this persona.")
    failed: int = Field(default=0, description="Failed runs by this persona.")


class RecentFailure(BaseModel):
    """A recent failed run for the dashboard failures tile."""

    invocation_id: str
    invoked_at: str = Field(description="ISO 8601 timestamp when the run started.")
    persona: str | None = None
    repo: str | None = None
    topic: str | None = None
    error_message: str | None = None


class TopRepo(BaseModel):
    """Per-repository run count."""

    repo: str = Field(description="Repository identifier (owner/repo).")
    total: int = Field(default=0, description="Total runs targeting this repo in the window.")


class ActiveRun(BaseModel):
    """A currently in-progress run."""

    invocation_id: str
    invoked_at: str = Field(description="ISO 8601 timestamp when the run started.")
    persona: str | None = None
    repo: str | None = None
    topic: str | None = None


class Spend(BaseModel):
    """Aggregated spend for the time window."""

    total_cost_usd: float = Field(default=0.0, description="Total spend in USD across all runs in the window.")
    total_tokens: int = Field(default=0, description="Total tokens (input + output) across all runs.")
    total_calls: int = Field(default=0, description="Total Bedrock API calls across all runs.")


class StatsResponse(BaseModel):
    """Full stats response for the agent run dashboard.

    Combines DynamoDB invocation aggregates with Postgres cost data.
    """

    window_days: int = Field(description="Number of days in the stats window (1-30).")
    active_runs: list[ActiveRun] = Field(default_factory=list, description="Currently in-progress runs.")
    today: TodayCounts = Field(default_factory=TodayCounts, description="Today's aggregate counts.")
    daily: list[DailyEntry] = Field(default_factory=list, description="Per-day breakdown for the window.")
    by_persona: list[PersonaStats] = Field(default_factory=list, description="Per-persona breakdown.")
    recent_failures: list[RecentFailure] = Field(
        default_factory=list,
        description="Most recent failed runs (up to 10).",
    )
    top_repos: list[TopRepo] = Field(default_factory=list, description="Top repositories by run count (up to 10).")
    spend: Spend | None = Field(default=None, description="Cost aggregates for the window. Null if cost data unavailable.")
