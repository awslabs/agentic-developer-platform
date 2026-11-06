"""Select one known native revision per logical work or charge record."""

from insights import projection


def select_logical_revisions(facts, as_of):
    """Choose the latest recorded revision before deduplicating realm copies.

    A correction can change a prior effective event. A tie between different
    revisions at the same recording time is ambiguous rather than arbitrarily
    ordered by an opaque revision string.
    """
    cutoff = projection._timestamp(as_of)
    candidates = list(facts)
    latest_times = {}
    for fact in candidates:
        identity = projection._identity(fact["subject_ref"])
        recorded = projection._timestamp(fact["recorded_at"])
        if recorded > cutoff:
            continue
        key = identity[:5]
        if key not in latest_times or latest_times[key] < recorded:
            latest_times[key] = recorded
    latest = {}
    for fact in candidates:
        identity = projection._identity(fact["subject_ref"])
        key = identity[:5]
        if projection._timestamp(fact["recorded_at"]) != latest_times.get(key):
            continue
        if key in latest and latest[key] != identity:
            raise ValueError("ambiguous logical revisions at recording time")
        latest[key] = identity
    return [fact for fact in candidates
            if (identity := projection._identity(fact["subject_ref"]))[:5] in latest
            and identity == latest[identity[:5]]
            and projection._timestamp(fact["recorded_at"]) <= cutoff
            and projection._timestamp(fact["effective_at"]) <= cutoff]
