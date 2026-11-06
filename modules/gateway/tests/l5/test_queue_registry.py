"""An independent service install becomes visible without replacing the gateway."""

from unittest.mock import MagicMock

import pytest

from src import l5_queue_registry


@pytest.mark.parametrize("service", sorted(l5_queue_registry.SERVICES))
async def test_registry_resolves_only_selected_service(monkeypatch, service):
    monkeypatch.setenv("L5_DEPLOYMENT_ENV", "test")
    for name in ["L5_WORK_QUEUE_URL", *(f"L5_{s.upper()}_WORK_QUEUE_URL" for s in l5_queue_registry.SERVICES)]:
        monkeypatch.delenv(name, raising=False)
    suffix = "" if service == "security" else "-" + service.replace("_", "-")
    url = f"https://sqs.us-east-1.amazonaws.com/000000000000/adp-test-l5{suffix}-work"
    ssm, sqs = MagicMock(), MagicMock()
    ssm.get_parameter.return_value = {
        "Parameter": {
            "ARN": f"arn:aws:ssm:us-east-1:000000000000:parameter/adp/test/l5-services/{service}/work-queue-url",
            "Value": url,
        }
    }
    monkeypatch.setattr(l5_queue_registry.boto3, "client", lambda name, **_: {"ssm": ssm, "sqs": sqs}[name])
    publish = l5_queue_registry.publisher(service)
    await publish({"work_id": "opaque"}, 17)
    assert sqs.send_message.call_args.kwargs["QueueUrl"] == url
    assert sqs.send_message.call_args.kwargs["DelaySeconds"] == 17
    ssm.get_parameter.assert_called_with(Name=f"/adp/test/l5-services/{service}/work-queue-url")
    sqs.reset_mock()
    ssm.get_parameter.return_value["Parameter"]["Value"] = "https://sqs.us-east-1.amazonaws.com/000000000000/foreign"
    with pytest.raises(ValueError, match="differs"):
        await publish({"work_id": "opaque"}, 0)
    sqs.send_message.assert_not_called()
