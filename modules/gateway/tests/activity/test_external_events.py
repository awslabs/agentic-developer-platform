"""EXT01-t1: linked actions are work; assignments and bot work are not human actions."""

from dataclasses import replace

from src.activity.external_events import ProviderEvent, classify_events


def event(kind, event_id, *, provider="github", repository="team-one/tool", actor_id="17", actor_kind="human", on_behalf_of=None):
    return ProviderEvent(
        provider=provider,
        kind=kind,
        event_id=event_id,
        source_url=f"https://example.invalid/{provider}/{repository}/{kind}/{event_id}",
        repository=repository,
        actor_id=actor_id,
        actor_kind=actor_kind,
        occurred_at="2026-10-01T10:00:00Z",
        on_behalf_of=on_behalf_of,
    )


def test_authored_actions_across_orgs_and_providers_are_human_work():
    actions = [
        event("commit", "sha-1"),
        event("pull_request", "pr-1", repository="team-two/tool"),
        event("review", "review-1"),
        event("comment", "comment-1", provider="gitlab", actor_id="42"),
    ]
    result = classify_events(actions, {"github": {"17"}, "gitlab": {"42"}})
    assert [(item.event.kind, item.attribution, item.human_work) for item in result] == [
        (kind, "human", True) for kind in ("commit", "pull_request", "review", "comment")
    ]


def test_assignment_only_and_unlinked_actors_do_not_prove_work():
    actions = [event("assignment", "assigned"), event("comment", "stranger", actor_id="18")]
    assert classify_events(actions, {"github": {"17"}}) == []
    assert classify_events([event("commit", "sha-1")], {}) == []


def test_bot_and_agent_events_keep_attribution_without_counting_as_human_work():
    actions = [
        event("comment", "bot-1", actor_id="bot", actor_kind="bot", on_behalf_of="17"),
        event("commit", "agent-1", actor_id="agent", actor_kind="agent", on_behalf_of="17"),
        event("comment", "unrelated-bot", actor_id="bot", actor_kind="bot"),
    ]
    result = classify_events(actions, {"github": {"17"}})
    assert [(item.attribution, item.human_work) for item in result] == [("bot", False), ("agent", False)]


def test_source_id_or_url_replays_are_not_counted_twice():
    original = event("review", "review-1")
    actions = [
        original,
        replace(original, source_url="https://example.invalid/alternate-review"),
        replace(original, event_id="review-2"),
        event("review", "review-1", repository="team-two/tool"),
    ]
    assert [item.event.repository for item in classify_events(actions, {"github": {"17"}})] == ["team-one/tool", "team-two/tool"]
