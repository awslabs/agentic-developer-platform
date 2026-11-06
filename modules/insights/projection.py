"""Read-only aggregation of native work and ledger facts with business context."""

from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation


REFERENCE_FIELDS = (
    "tenant_id", "project_id", "source_connection_id",
    "record_type", "record_id", "record_revision",
)


def _identity(reference):
    if (not isinstance(reference, dict) or set(reference) != set(REFERENCE_FIELDS)
            or any(not isinstance(reference[field], str) or not reference[field]
                   for field in REFERENCE_FIELDS)
            or reference["record_revision"] in ("latest", "null")):
        raise ValueError("fact requires an exact qualified native reference")
    return tuple(reference[field] for field in REFERENCE_FIELDS)


def aggregate_work_cost(rows, portfolio_ids=()):
    """Aggregate authorized (native fact, associated Portfolio IDs) rows once per fact.

    The caller resolves and authorizes the associations before passing rows here.
    Filtering uses every association, but the original qualified fact identity is
    the unit of aggregation; Portfolio summaries must not be summed together.
    """
    selected = set(portfolio_ids)
    facts = {}
    associations = defaultdict(set)
    for fact, portfolios in rows:
        identity = _identity(fact.get("subject_ref"))
        if not isinstance(portfolios, (list, tuple, set, frozenset)) or any(
                not isinstance(portfolio, str) or not portfolio for portfolio in portfolios):
            raise ValueError("Portfolio associations must be nonempty identifiers")
        portfolios = set(portfolios)
        if selected and not portfolios.intersection(selected):
            continue
        kind = fact.get("kind")
        if kind not in ("work", "cost"):
            raise ValueError("only native work and cost facts can be aggregated")
        value = (kind,)
        if kind == "cost":
            currency = fact.get("currency")
            if not isinstance(currency, str) or not currency:
                raise ValueError("cost requires a currency")
            try:
                amount = Decimal(str(fact["amount"]))
            except (KeyError, InvalidOperation, ValueError):
                raise ValueError("cost requires a decimal amount") from None
            if not amount.is_finite():
                raise ValueError("cost requires a finite amount")
            value = (kind, amount, currency)
        if identity in facts and facts[identity] != value:
            raise ValueError("conflicting versions of the same native fact")
        facts[identity] = value
        associations[identity].update(portfolios)

    totals = defaultdict(Decimal)
    for value in facts.values():
        if value[0] == "cost":
            totals[value[2]] += value[1]
    return {
        "work_count": sum(value[0] == "work" for value in facts.values()),
        "cost_by_currency": dict(totals),
        "associations": [
            {"subject_ref": dict(zip(REFERENCE_FIELDS, identity)),
             "portfolio_ids": sorted(associations[identity])}
            for identity in facts
        ],
    }


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("relationship time must be a timezone-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("relationship time must be a timezone-aware timestamp") from None
    if parsed.tzinfo is None:
        raise ValueError("relationship time must be a timezone-aware timestamp")
    return parsed


def select_dated_relationships(relationships, effective_at, known_at):
    """Choose the latest known version of each relationship, then check its validity."""
    effective_time = _timestamp(effective_at)
    known_time = _timestamp(known_at)
    versions = {}
    for relationship in relationships:
        source = _identity(relationship["from_ref"])
        target = _identity(relationship["to_ref"])
        required = ("relationship_id", "relationship_revision", "relationship_type",
                    "source_connection_id", "mapping_version")
        if any(not isinstance(relationship.get(field), str) or not relationship[field]
               for field in required):
            raise ValueError("relationship requires identity and mapping version")
        if source[0] != target[0]:
            raise ValueError("cross-tenant relationship")
        recorded = _timestamp(relationship["recorded_at"])
        start = _timestamp(relationship["valid_from"])
        end = _timestamp(relationship["valid_to"]) if relationship.get("valid_to") else None
        if end is not None and end <= start:
            raise ValueError("relationship has invalid effective interval")
        if recorded > known_time:
            continue
        key = (source[0], source[1], target[1], relationship["source_connection_id"],
               relationship["relationship_type"], relationship["relationship_id"])
        previous = versions.get(key)
        if previous is not None and previous[1] == recorded and previous[0] != relationship:
            raise ValueError("ambiguous relationship revisions at recording time")
        if previous is None or previous[1] < recorded:
            versions[key] = (relationship, recorded, start, end)
    return [relationship for relationship, _, start, end in versions.values()
            if start <= effective_time and (end is None or effective_time < end)]


def resolve_dated_context(fact, relationships, regroup_at=None):
    """Join explicit native→Product and Portfolio→Product links without rewriting facts.

    Historical context is known when the native fact was recorded. Explicit
    regrouping uses a caller-supplied date for both membership and knowledge.
    """
    original = _identity(fact["subject_ref"])
    when = regroup_at if regroup_at is not None else fact["effective_at"]
    known = regroup_at if regroup_at is not None else fact["recorded_at"]
    selected = select_dated_relationships(relationships, when, known)
    product_links = [link for link in selected
                     if link["relationship_type"] == "work_product"
                     and _identity(link["from_ref"]) == original
                     and link["to_ref"]["record_type"] == "product"]
    products = [link["to_ref"] for link in product_links]
    memberships = [link for link in selected
                   if link["relationship_type"] == "portfolio_product"
                   and link["from_ref"]["record_type"] == "portfolio"
                   and link["to_ref"]["record_type"] == "product"
                   and any(_identity(link["to_ref"])[:5] == _identity(product)[:5]
                           for product in products)]
    return {
        "subject_ref": fact["subject_ref"],
        "grouping": "current_regrouping" if regroup_at is not None else "historical",
        "effective_at": when,
        "product_refs": products,
        "portfolio_ids": sorted({link["from_ref"]["record_id"] for link in memberships}),
        "relationship_versions": [
            {"relationship_id": link["relationship_id"],
             "relationship_revision": link["relationship_revision"],
             "mapping_version": link["mapping_version"],
             "from_ref": link["from_ref"], "to_ref": link["to_ref"]}
            for link in product_links + memberships
        ],
    }


def query_authorized_projection(facts, relationships, allocations, *, tenant_id,
                                project_ids, can_read_project, portfolio_ids=(),
                                regroup_at=None, contributions=()):
    """Compose an access-scoped view from source-owned facts and approved links.

    The access check covers every original Project and every linked reference;
    links never grant access. No inaccessible count or source identity is returned.
    """
    project_ids = set(project_ids)
    authorized = {project_id for project_id in project_ids
                  if can_read_project(tenant_id, project_id)}
    incomplete = len(authorized) != len(project_ids)

    def permitted(reference):
        return (_identity(reference)[0] == tenant_id
                and reference["project_id"] in authorized)

    visible_facts = []
    native_facts = {}
    for fact in facts:
        reference = fact["subject_ref"]
        if not permitted(reference) or (fact.get("tenant_id", tenant_id) != tenant_id
                                        or fact.get("project_id", reference["project_id"]) != reference["project_id"]):
            incomplete = True
            continue
        identity = _identity(reference)
        if identity in native_facts:
            if native_facts[identity] != fact:
                raise ValueError("conflicting versions of the same native fact")
            continue
        native_facts[identity] = fact
        visible_facts.append(fact)

    visible_links = []
    for link in relationships:
        references = [link["from_ref"], link["to_ref"], *link.get("source_refs", [])]
        if all(permitted(reference) for reference in references):
            visible_links.append(link)
        else:
            incomplete = True

    visible_contributions = []
    for contribution in contributions:
        reference = contribution["subject_ref"]
        data = contribution["source_snapshot"]
        data = data.get("data", data)
        references = [reference, data["product_ref"], data["initiative_ref"]]
        references.extend(link["work_ref"] for link in data.get("work_links", []))
        if all(permitted(item) for item in references):
            visible_contributions.append((contribution, data))
        else:
            incomplete = True

    contexts = []
    rows = []
    selected_portfolios = set(portfolio_ids)
    for fact in visible_facts:
        when = regroup_at if regroup_at is not None else fact["effective_at"]
        effective = _timestamp(when)
        known = _timestamp(regroup_at if regroup_at is not None else fact["recorded_at"])
        latest = {}
        for contribution, data in visible_contributions:
            recorded = _timestamp(contribution["recorded_at"])
            if recorded <= known:
                key = _identity(contribution["subject_ref"])[:5]
                if key in latest and latest[key][2] == recorded and latest[key][0] != contribution:
                    raise ValueError("ambiguous contribution revisions at recording time")
                if key not in latest or latest[key][2] < recorded:
                    latest[key] = (contribution, data, recorded)
        fact_links = list(visible_links)
        for contribution, data, _ in latest.values():
            for work_link in data.get("work_links", []):
                if _identity(work_link["work_ref"]) != _identity(fact["subject_ref"]):
                    continue
                if (_timestamp(work_link["valid_from"]) > effective
                        or (work_link.get("valid_to") and effective >= _timestamp(work_link["valid_to"]))):
                    continue
                link = {
                    "relationship_type": "work_product",
                    "relationship_id": contribution["subject_ref"]["record_id"] + ":" + work_link["work_ref"]["record_id"],
                    "relationship_revision": contribution["subject_ref"]["record_revision"],
                    "source_connection_id": contribution["subject_ref"]["source_connection_id"],
                    "from_ref": work_link["work_ref"], "to_ref": data["product_ref"],
                    "mapping_version": contribution["mapping_version"],
                    "valid_from": work_link["valid_from"],
                    "recorded_at": contribution["recorded_at"],
                }
                ends = [value for value in (work_link.get("valid_to"), data.get("valid_to")) if value]
                if ends:
                    link["valid_to"] = min(ends, key=_timestamp)
                fact_links.append(link)
        context = resolve_dated_context(fact, fact_links, regroup_at=regroup_at)
        if not context["product_refs"]:
            incomplete = True
        for contribution, data, _ in latest.values():
            if (_timestamp(data["valid_from"]) > effective
                    or (data.get("valid_to") and effective >= _timestamp(data["valid_to"]))):
                continue
            if not any(_identity(link["work_ref"]) == _identity(fact["subject_ref"])
                       and _timestamp(link["valid_from"]) <= effective
                       and (not link.get("valid_to") or effective < _timestamp(link["valid_to"]))
                       for link in data.get("work_links", [])):
                continue
            if not any(_identity(product)[:5] == _identity(data["product_ref"])[:5]
                       for product in context["product_refs"]):
                continue
            context.setdefault("initiative_refs", []).append(data["initiative_ref"])
            context.setdefault("contribution_refs", []).append(contribution["subject_ref"])
        context.setdefault("initiative_refs", [])
        context.setdefault("contribution_refs", [])
        if selected_portfolios and not selected_portfolios.intersection(context["portfolio_ids"]):
            continue
        contexts.append(context)
        rows.append((fact, context["portfolio_ids"]))

    result = aggregate_work_cost(rows)
    included = {_identity(context["subject_ref"]) for context in contexts}
    amounts = {}
    for fact in visible_facts:
        identity = _identity(fact["subject_ref"])
        if identity not in included or fact["kind"] != "cost":
            continue
        amount = Decimal(str(fact["amount"]))
        currency = fact["currency"]
        if identity in amounts and amounts[identity] != (amount, currency):
            raise ValueError("conflicting versions of the same native fact")
        amounts[identity] = (amount, currency)

    allocated = defaultdict(Decimal)
    by_fact = defaultdict(Decimal)
    seen_allocations = set()
    allocation_versions = []
    unknown_allocation_currencies = set()
    for allocation in allocations:
        original = _identity(allocation["source_ref"])
        target = allocation["target_ref"]
        if not permitted(allocation["source_ref"]) or not permitted(target):
            incomplete = True
            if original in amounts:
                unknown_allocation_currencies.add(amounts[original][1])
            continue
        if original not in amounts:
            continue
        if target["record_type"] not in ("product", "initiative", "contribution"):
            raise ValueError("allocation target must be an approved business scope")
        allocation_id = allocation.get("allocation_id")
        policy = allocation.get("allocation_policy_version")
        basis = allocation.get("allocation_basis")
        if any(not isinstance(value, str) or not value for value in (allocation_id, policy, basis)):
            raise ValueError("allocation requires identity, policy version and basis")
        key = (original, allocation_id)
        if key in seen_allocations:
            raise ValueError("duplicate cost allocation")
        seen_allocations.add(key)
        amount, currency = amounts[original]
        if currency != allocation.get("currency"):
            raise ValueError("allocation currency differs from its native fact")
        try:
            portion = Decimal(str(allocation["amount"]))
        except (KeyError, InvalidOperation, ValueError):
            raise ValueError("allocation requires a decimal amount") from None
        if not portion.is_finite() or portion < 0:
            raise ValueError("allocation must be finite and nonnegative")
        by_fact[original] += portion
        if by_fact[original] > amount:
            raise ValueError("cost allocation exceeds native fact")
        allocated[currency] += portion
        allocation_versions.append({"allocation_id": allocation_id,
                                    "allocation_policy_version": policy,
                                    "allocation_basis": basis,
                                    "source_ref": allocation["source_ref"],
                                    "target_ref": target})

    unallocated = defaultdict(Decimal)
    for identity, (amount, currency) in amounts.items():
        if currency not in unknown_allocation_currencies:
            unallocated[currency] += amount - by_fact[identity]
    result.update(coverage="incomplete" if incomplete else "complete",
                  grouping="current_regrouping" if regroup_at is not None else "historical",
                  contexts=contexts, allocated_by_currency=dict(allocated),
                  unallocated_by_currency=dict(unallocated),
                  allocation_versions=allocation_versions)
    return result
