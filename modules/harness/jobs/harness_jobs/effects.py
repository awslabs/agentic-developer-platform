"""Provider effects from exact approved provider/action pairs; unknown may create."""

import re
from enum import Enum


class CallEffect(Enum):
    """What a planned provider call can do to an allocation's contents.

    The distinction the seal needs. A closed allocation must still be tearable-down and
    inspectable -- refusing those would mean an allocation could be sealed and then
    never cleaned up, which keeps resources billing for the opposite reason -- so the
    withdrawal of authority applies to calls that can CREATE, not to calls that destroy
    or ask.
    """

    CREATES = "creates"
    """The call may bring a billable resource into existence."""

    REMOVES = "removes"
    """The call destroys, detaches or releases. Always permitted: teardown must run."""

    OBSERVES = "observes"
    """The call only asks. Permitted for the same reason, and it changes nothing."""

    UNRECOGNIZED = "unrecognized"
    """The verb is not one this module knows. Treated as CREATES everywhere it matters.

    The fail-closed default, and it is a deliberate asymmetry. An unrecognized teardown
    verb is refused into a sealed allocation with a message naming the verb, which an
    operator can read and fix by naming the step's kind in the vocabulary below. An
    unrecognized creating verb allowed through costs a running resource nothing knows
    about and a released budget that would have paid for it. The first failure is
    visible and recoverable; the second is silent and expensive.
    """


# Exact, reviewed actions only. A token such as "read" inside an arbitrary
# approved operation name is not evidence that its provider hook is read-only.
_READ_ONLY_KINDS = frozenset(
    {
        "describe_cluster",
        "describecluster",
        "eks:describecluster",
        "ec2:describeinstances",
        "list_disks",
        "list_instances",
        "get_instance",
        "get_snapshot",
        "status",
    }
)
_REMOVAL_KINDS = frozenset(
    {
        "delete_cluster",
        "delete_disk",
        "delete_volume",
        "delete_network",
        "terminate-instances",
        "ec2:terminateinstances",
        "eks:deletecluster",
        "ec2:deletevolume",
        "ec2:deletesnapshot",
        "ec2:deletenetworkinterface",
        "elasticloadbalancing:deleteloadbalancer",
    }
)

_READ_ONLY_ACTIONS = {
    "aws": _READ_ONLY_KINDS,
    "gcp": frozenset({"compute.instances.get", "compute.instances.list"}),
    "superplane-aws": frozenset({"verify-resource-inventory"}),
}
_REMOVAL_ACTIONS = {
    "aws": _REMOVAL_KINDS,
    "gcp": frozenset({"compute.instances.delete"}),
    "superplane-kubernetes": frozenset({"delete-controller-component", "revoke-grant"}),
    "superplane-aws": frozenset({"revoke-grant", "revoke-network-prerequisite"}),
    "superplane-terraform": frozenset({"apply-reviewed-destroy"}),
    "superplane-governance": frozenset(
        {"block-governed-admission", "drain-governed-workloads"}
    ),
    "superplane-registry": frozenset({"unregister-workspace"}),
}

_CREATING_VERBS = frozenset(
    {
        "add",
        "allocate",
        "apply",
        "associate",
        "attach",
        "clone",
        "copy",
        "create",
        "deploy",
        "enable",
        "ensure",
        "grant",
        "import",
        "insert",
        "launch",
        "modify",
        "patch",
        "post",
        "provision",
        "put",
        "register",
        "reserve",
        "resize",
        "restore",
        "run",
        "scale",
        "set",
        "start",
        "update",
    }
)

# Words in a provider kind, however it is punctuated or cased. `create_cluster`,
# `create-cluster`, `ec2:RunInstances`, `compute.instances.insert`, `UPDATE_NODEGROUP`.
# The three alternatives are an acronym run (`EC2`, `RDS`), a capitalized word
# (`Run`, `Instances`) and a lowercase word -- so CamelCase splits into its words
# instead of arriving as one unknown token.
_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+")


def call_effect(operation_kind: object, *, provider: str | None = None) -> CallEffect:
    """Grant non-creating authority only to exact reviewed action names.

    Unrecognized/ambiguous actions remain creation-capable. Adding a provider
    action requires a reviewed allowlist entry and matching provider-hook tests.
    Python and the durable epoch trigger use the same read-only allowlist.
    """
    if not isinstance(operation_kind, str) or not operation_kind.strip():
        return CallEffect.UNRECOGNIZED
    kind = operation_kind.lower()
    if kind in _READ_ONLY_ACTIONS.get(provider, ()):
        return CallEffect.OBSERVES
    if kind in _REMOVAL_ACTIONS.get(provider, ()):
        return CallEffect.REMOVES
    if {word.lower() for word in _WORDS.findall(operation_kind)} & _CREATING_VERBS:
        return CallEffect.CREATES
    return CallEffect.UNRECOGNIZED


def may_create(operation_kind: object, *, provider: str | None = None) -> bool:
    """Whether a call under this kind must be fenced by a seal.

    `CREATES` and `UNRECOGNIZED`, for the reason `CallEffect.UNRECOGNIZED` states: the
    question is "could this start costing money", and "we do not know" has to answer
    yes.
    """
    return call_effect(operation_kind, provider=provider) in (
        CallEffect.CREATES,
        CallEffect.UNRECOGNIZED,
    )
