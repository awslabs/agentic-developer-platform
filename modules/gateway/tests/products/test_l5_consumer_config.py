import json

import pytest

from src.l5_consumer_config import consumer_targets


@pytest.mark.parametrize(
    "origin",
    [
        "https://public.example",
        "http://169.254.169.254",
        "http://localhost",
        "http://user:pass@context.private.svc",
        "http://context.private.svc/path",
        "http://context.private.svc?key=1",
        "http://context.private.svc:0",
        "http://context.private.svc\n",
        "http://context.private.svc'",
        1,
    ],
)
def test_private_targets_reject_nonservice_origins(origin):
    with pytest.raises(ValueError):
        consumer_targets(json.dumps({"product-context": origin}))


def test_private_targets_default_disabled_and_retain_selected_destinations():
    assert consumer_targets("{}") == {}
    targets = {"product-context": "http://context.private.svc:8080", "engineering-insights": "https://receiver.insights.svc.cluster.local"}
    assert consumer_targets(json.dumps(targets)) == targets
    with pytest.raises(ValueError):
        consumer_targets('{"unknown":"http://context.private.svc"}')
