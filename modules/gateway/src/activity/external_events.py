"""Classify provider events before adding them to a personal work timeline."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

Provider = Literal["github", "gitlab"]
EventKind = Literal["commit", "pull_request", "review", "comment", "assignment"]
ActorKind = Literal["human", "bot", "agent"]


@dataclass(frozen=True)
class ProviderEvent:
    provider: Provider
    kind: EventKind
    event_id: str
    source_url: str
    repository: str
    actor_id: str
    actor_kind: ActorKind
    occurred_at: str
    on_behalf_of: str | None = None


@dataclass(frozen=True)
class ClassifiedEvent:
    event: ProviderEvent
    attribution: ActorKind
    human_work: bool


def classify_events(events: list[ProviderEvent], verified_user_ids: Mapping[Provider, set[str]]) -> list[ClassifiedEvent]:
    """Keep only actual actions attributable to verified identities, never assignments.

    A bot/agent event may be displayed as related work only when its trusted
    provenance identifies a verified human; it never counts as that human's action.
    Repository entitlement is checked by the caller before invoking this function.
    """
    result: list[ClassifiedEvent] = []
    seen_ids: set[tuple[str, str, str, str]] = set()
    seen_urls: set[tuple[str, str, str]] = set()
    for event in events:
        identities = verified_user_ids.get(event.provider, set())
        if event.kind == "assignment" or not identities:
            continue
        if event.actor_kind == "human":
            if event.actor_id not in identities:
                continue
        elif event.on_behalf_of not in identities:
            continue

        identity = (event.provider, event.repository, event.kind, event.event_id)
        source = (event.provider, event.kind, event.source_url)
        if identity in seen_ids or (event.provider == "github" and source in seen_urls):
            continue
        seen_ids.add(identity)
        if event.provider == "github":
            seen_urls.add(source)
        result.append(ClassifiedEvent(event=event, attribution=event.actor_kind, human_work=event.actor_kind == "human"))
    return result
