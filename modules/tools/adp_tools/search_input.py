"""Public bounded Web Search input shared by the façade and provider."""

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

DOMAIN = (
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


class DomainFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    include: list[str] = Field(default_factory=list, max_length=100)
    exclude: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_domains(self):
        for domain in self.include + self.exclude:
            if not re.fullmatch(DOMAIN, domain) or len(domain) > 253:
                raise ValueError("Invalid domain")
        return self


class PublishedDateFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None

    @model_validator(mode="after")
    def validate_dates(self):
        for date in (self.from_, self.to):
            if date is not None:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", date):
                    raise ValueError("Date must be ISO-8601 UTC")
                datetime.fromisoformat(date.replace("Z", "+00:00"))
        if self.from_ and self.to and self.from_ > self.to:
            raise ValueError("Inverted date range")
        return self


class Filters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domainFilter: DomainFilter | None = None
    publishedDateFilter: PublishedDateFilter | None = None


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=200)
    maxResults: int = Field(default=10, ge=1, le=25, strict=True)
    filters: Filters | None = None

    @model_validator(mode="after")
    def validate_query(self):
        if not self.query.strip():
            raise ValueError("Empty search query")
        return self
