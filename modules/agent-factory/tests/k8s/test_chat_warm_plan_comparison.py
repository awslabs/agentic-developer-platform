"""A default-plan comparison must reject additions, mutations and deletions."""

from copy import deepcopy

import pytest

from compare_chat_warm_plans import compare


BASELINE = {
    "resources": {"aws_sqs_queue.tasks": {"change": {"actions": ["create"], "after": {"name": "tasks.fifo"}}}},
    "outputs": {"queue_url": {"after_unknown": True}},
}


def test_identical_plans_pass():
    compare(BASELINE, deepcopy(BASELINE))


@pytest.mark.parametrize("change", ["addition", "mutation", "deletion", "output"])
def test_disabled_plan_changes_are_rejected(change):
    candidate = deepcopy(BASELINE)
    if change == "addition":
        candidate["resources"]["aws_sqs_queue.warm"] = {"change": {"actions": ["create"]}}
    elif change == "mutation":
        candidate["resources"]["aws_sqs_queue.tasks"]["change"]["after"]["name"] = "warm.fifo"
    elif change == "deletion":
        del candidate["resources"]["aws_sqs_queue.tasks"]
    else:
        candidate["outputs"]["queue_url"] = {"after": "changed"}
    with pytest.raises(RuntimeError, match="Default plans differ"):
        compare(BASELINE, candidate)
