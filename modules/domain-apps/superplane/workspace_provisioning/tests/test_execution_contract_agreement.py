"""The copied descriptor contract has not drifted from the authoritative one.

`execution_contract.py` is a structural copy of `harness_jobs.execution_descriptors`,
for the reason that module's docstring gives. A copy checked only against itself is
worthless: if it drifts, this package composes retirement plans that the real admission
layer refuses — or, worse, plans whose encoding hashes to a different payload digest
than the one approval bound, which admission reads as a changed request under a used
idempotency key.

So every assertion here imports the real module (tests only; see `tests/__init__.py`)
and compares. The comparisons are chosen to be the ones that can actually cost
something:

* **bounds**, because a copy with a larger limit accepts a plan admission rejects, and
  a copy with a smaller one refuses a retirement that would have been admissible;
* **field names and their order**, because the encoding is hashed, so field order is
  part of the approved identity rather than a formatting detail;
* **byte-identical output**, asserted directly rather than inferred from the two
  previous points;
* **round-tripping through the authoritative parser**, which is the real validator this
  package's output must survive — including the refusals the copy deliberately does not
  re-implement.

No database, no provider and no network here: this is a comparison of two Python
modules.
"""

import json
from dataclasses import fields

import pytest
from harness_jobs import execution_descriptors as authoritative

from workspace_provisioning import execution_contract as copy


def test_the_bounds_are_identical():
    """A differing bound means one of the two refuses what the other admits."""
    assert copy.MAX_EXECUTION_STEPS == authoritative.MAX_EXECUTION_STEPS
    assert copy.MAX_EXECUTION_PLAN_BYTES == authoritative.MAX_EXECUTION_PLAN_BYTES
    assert copy.MAX_DESCRIPTOR_VALUE_LENGTH == authoritative.MAX_DESCRIPTOR_VALUE_LENGTH


def test_the_descriptor_fields_and_their_order_are_identical():
    """Order too: the encoding is hashed into the approved payload digest."""
    assert [field.name for field in fields(copy.ExecutionStep)] == [
        field.name for field in fields(authoritative.ExecutionStep)
    ]


def test_the_encoding_is_byte_identical_to_the_authoritative_encoder():
    """The property that actually matters, asserted directly rather than inferred."""
    steps = [
        ("block-admission", "superplane-governance", "block", '{"a":1}'),
        ("delete-ns", "superplane-kubernetes", "delete-namespace", '{"uid":"u"}'),
        # Non-ASCII: both encoders declare `ensure_ascii=False`, and a copy that
        # escaped instead would produce a different digest for the same plan.
        ("verify", "superplane-aws", "verify", '{"note":"café"}'),
    ]
    mine = copy.encode_execution_steps(
        [copy.ExecutionStep(*values) for values in steps]
    )
    theirs = authoritative.encode_execution_steps(
        [authoritative.ExecutionStep(*values) for values in steps]
    )
    assert mine == theirs


def test_a_composed_plan_reparses_through_the_authoritative_validator():
    """Valid local output must survive the authoritative runtime validator."""
    steps = [
        copy.ExecutionStep("one", "superplane-governance", "block", "{}"),
        copy.ExecutionStep("two", "superplane-registry", "unregister", "{}"),
    ]
    parsed = authoritative.parse_execution_steps(copy.encode_execution_steps(steps))
    assert [
        (step.step_id, step.provider, step.operation_kind, step.target)
        for step in parsed
    ] == [
        (step.step_id, step.provider, step.operation_kind, step.target)
        for step in steps
    ]


def test_both_parsers_reject_invalid_descriptors():
    """Both runtime boundaries refuse the same malformed wire inputs."""
    over_limit = "x" * (authoritative.MAX_DESCRIPTOR_VALUE_LENGTH + 1)
    for payload, reason in [
        ("[]", "an empty plan"),
        (
            json.dumps(
                [
                    {
                        "step_id": "dup",
                        "provider": "p",
                        "operation_kind": "k",
                        "target": "t",
                    }
                ]
                * 2
            ),
            "a duplicate step id",
        ),
        (
            json.dumps(
                [
                    {
                        "step_id": "blank",
                        "provider": "p",
                        "operation_kind": "k",
                        "target": "   ",
                    }
                ]
            ),
            "a blank descriptor value",
        ),
        (
            json.dumps(
                [
                    {
                        "step_id": "long",
                        "provider": "p",
                        "operation_kind": "k",
                        "target": over_limit,
                    }
                ]
            ),
            "an oversized descriptor value",
        ),
    ]:
        for module in (copy, authoritative):
            with pytest.raises(ValueError, match="."):
                module.parse_execution_steps(payload)


@pytest.mark.parametrize(
    "values",
    [
        [],
        [("dup", "p", "k", "t")] * 2,
        [("blank", "p", "k", "   ")],
        [("nul", "p", "k", "a\x00b")],
        [("number", "p", "k", 3)],
        [("long", "p", "k", "x" * (authoritative.MAX_DESCRIPTOR_VALUE_LENGTH + 1))],
        [(str(i), "p", "k", "x" * 2048) for i in range(9)],
    ],
)
def test_both_encoders_refuse_before_a_malformed_plan_can_be_approved(values):
    for module in (copy, authoritative):
        with pytest.raises(ValueError):
            module.encode_execution_steps(
                [module.ExecutionStep(*row) for row in values]
            )


def test_duplicate_wire_fields_are_not_normalized_into_valid_descriptors():
    payload = '[{"step_id":"one","provider":"p","provider":"other","operation_kind":"k","target":"t"}]'
    for module in (copy, authoritative):
        with pytest.raises(ValueError, match="Duplicate descriptor field"):
            module.parse_execution_steps(payload)
