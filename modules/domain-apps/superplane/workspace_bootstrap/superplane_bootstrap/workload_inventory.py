"""Bounded workload reads and original, pre-admission system ownership evidence."""

from .errors import BootstrapRefused
from .kube_grants import _payload
from .retirement_fence import WORKLOADS


def key(body):
    meta = body.get("metadata", {})
    result = (
        body.get("kind"),
        meta.get("namespace", ""),
        meta.get("name"),
        meta.get("uid"),
    )
    if (
        not all(isinstance(v, str) for v in result)
        or not result[0]
        or not result[2]
        or not result[3]
    ):
        raise BootstrapRefused("workload inventory lacks immutable identity")
    return result


def read(grants):
    result, identities = [], set()
    for version, kind, _resource in WORKLOADS:
        token, seen = "", set()
        while True:
            grants._verify_transport()
            resource = grants.client.resources.get(api_version=version, kind=kind)
            page = _payload(
                resource.get(limit=100, **({"_continue": token} if token else {}))
            )
            if not isinstance(page, dict) or not isinstance(page.get("items"), list):
                raise BootstrapRefused("workload inventory is unanswered")
            for body in page["items"]:
                if not isinstance(body, dict) or body.get("kind", kind) != kind:
                    raise BootstrapRefused("workload inventory kind changed")
                body = {**body, "kind": kind}
                identity = key(body)
                if identity in identities or len(result) >= 10000:
                    raise BootstrapRefused(
                        "workload inventory is duplicated or exceeds its bound"
                    )
                identities.add(identity)
                result.append(body)
            token = page.get("metadata", {}).get("continue", "")
            if not token:
                break
            if not isinstance(token, str) or token in seen or len(seen) >= 100:
                raise BootstrapRefused("workload pagination is incomplete")
            seen.add(token)
    return result


def ownership_closure(bodies, roots):
    """Only exact roots or same-namespace children with matching owner UIDs."""
    owned = {tuple(row) for row in roots}
    unresolved = {key(body): body for body in bodies if key(body) not in owned}
    while unresolved:
        added = set()
        for identity, body in unresolved.items():
            references = body.get("metadata", {}).get("ownerReferences", [])
            if not isinstance(references, list):
                raise BootstrapRefused("workload owner references are malformed")
            if any(
                isinstance(ref, dict)
                and ref.get("controller") is True
                and (ref.get("kind"), identity[1], ref.get("name"), ref.get("uid"))
                in owned
                for ref in references
            ):
                added.add(identity)
        if not added:
            raise BootstrapRefused("workload has no original bootstrap ownership")
        owned.update(added)
        for identity in added:
            unresolved.pop(identity)
    return sorted(key(body) for body in bodies)


def capture_system_baseline(grants, journal, components, *, previous=None):
    """Called only during original paid managed bootstrap, before tenant admission.

    Names constrain the provider-created roots eligible for initial capture; their
    authority comes from the original dedicated cluster creation and closed
    bootstrap admission, not from recognizing names during retirement.
    """
    if journal.target.is_adopted or not journal.original_allocation_id:
        raise BootstrapRefused("system baseline requires original managed allocation")
    bodies = read(grants)
    if previous is not None:
        ownership_closure(bodies, [item["identity"] for item in previous["objects"]])
        return previous
    provider_roots = {
        ("Service", "default", "kubernetes"),
        ("Service", "kube-system", "kube-dns"),
        ("Deployment", "kube-system", "coredns"),
        ("DaemonSet", "kube-system", "aws-node"),
        ("DaemonSet", "kube-system", "kube-proxy"),
    }
    roots = [key(body) for body in bodies if key(body)[:3] in provider_roots]
    roots += [
        (
            item["desired"]["kind"],
            item["desired"]["metadata"].get("namespace", ""),
            item["desired"]["metadata"]["name"],
            item["identity"]["uid"],
        )
        for item in components.values()
        if item.get("phase") == "owned"
    ]
    ownership_closure(bodies, roots)
    return {
        "version": 1,
        "cluster_arn": journal.target.cluster_arn,
        "operation_id": journal.binding.operation_id,
        "original_allocation_id": journal.original_allocation_id,
        "generation": journal.generation,
        "objects": [
            {
                "identity": list(key(body)),
                "owner_references": body.get("metadata", {}).get("ownerReferences", []),
            }
            for body in sorted(bodies, key=key)
        ],
    }
