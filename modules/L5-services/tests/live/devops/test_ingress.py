"""Offline policy fixtures; no connection to a deployed target."""

import json
import time

import pytest

from ingress_policy import aws_read, read_ingress_policy, validate_policy_scope
from selector import qualify_journeys
from test_selector import RUN_ID, NOW, FakeReader, offline_drive, selector_case


@pytest.fixture
def policy_case(selector_case):
    manifest, path, directory, release_reader = selector_case
    scope = manifest["ingress_policy"]
    role_name = scope["role_arn"].split("/")[-1]
    responses = {
        ("sts", "get-caller-identity"): {"Account": scope["account_id"],
            "Arn": f"arn:aws:sts::{scope['account_id']}:assumed-role/{role_name}/session-example"},
        ("cloudfront", "get-distribution"): {"Distribution": {"Status": "Deployed",
            "DomainName": "edge.example.cloudfront.net",
            "DistributionConfig": {"Enabled": True, "Aliases": {"Items": ["gateway.example.org"]},
                "Origins": {"Items": [{"Id": scope["gateway_origin_id"], "DomainName": "api.example.org", "CustomOriginConfig": {"OriginProtocolPolicy": "https-only"}},
                                      {"Id": "frontend", "DomainName": "assets.example.org"}]},
                "CacheBehaviors": {"Items": [
                    {"PathPattern": "/api/*", "TargetOriginId": scope["gateway_origin_id"],
                     "AllowedMethods": {"Items": ["POST", "GET"]},
                     "ViewerProtocolPolicy": "redirect-to-https"}]},
                "DefaultCacheBehavior": {"TargetOriginId": "frontend"}}}},
        ("lambda", "list-function-url-configs"): {"FunctionUrlConfigs": []},
        ("lambda", "list-aliases"): {"Aliases": [{"Name": "active"}]},
        ("lambda", "get-policy"): None,
    }
    scope["allowed_invoke_statements"]["example-sso:active"] = []
    path.write_text(json.dumps(manifest))
    return manifest, path, directory, release_reader, responses


def policy_read(responses, calls):
    def read(scope, service, operation, *arguments, deadline, missing_ok=False):
        calls.append((service, operation, arguments))
        return responses.get((service, operation, arguments), responses[(service, operation)])
    return read


def test_independent_policy_readback_precedes_http_and_records_evidence(policy_case):
    manifest, path, directory, release_reader, responses = policy_case
    calls = []
    def read_policy(fixture, **bounds):
        result = read_ingress_policy(fixture, read=policy_read(responses, calls), **bounds)
        assert not active
        return result
    active = []
    def reader_factory(fixture, **bounds):
        active.append(1)
        return FakeReader(fixture, release_reader, **bounds)
    assert qualify_journeys(manifest["release"]["source_sha"], path, directory,
                            reader_factory=reader_factory, policy_reader=read_policy,
                            journey_runner=offline_drive, now=NOW) == RUN_ID
    result = json.loads((directory / f"devops-{RUN_ID}-journeys.json").read_text())
    assert result["live_checks"]["sso-gateway-only-ingress"]["aws_policy"]["verified_functions"] == [
        "example-sso", "example-sso:active"]
    assert calls[0][:2] == ("sts", "get-caller-identity")
    assert [call[1] for call in calls].count("get-policy") == 2


@pytest.mark.parametrize("tamper", [
    lambda responses: responses[("sts", "get-caller-identity")].update(Account="000000000000"),
    lambda responses: responses[("sts", "get-caller-identity")].update(Arn="arn:aws:sts::123456789012:assumed-role/other/session"),
    lambda responses: responses[("lambda", "list-function-url-configs")]["FunctionUrlConfigs"].append({"AuthType": "AWS_IAM"}),
    lambda responses: responses.__setitem__(("lambda", "get-policy"), {"Policy": json.dumps({"Statement": [{
        "Effect": "Allow", "Principal": "*", "Action": "lambda:InvokeFunction"}]})}),
    lambda responses: responses.__setitem__(("lambda", "get-policy"), {"Policy": json.dumps({"Statement": [{
        "Effect": "Allow", "Principal": {"AWS": "arn:aws:iam::123456789012:role/example-readonly"},
        "Action": "lambda:InvokeFunctionUrl"}]})}),
    lambda responses: responses[("lambda", "list-aliases")]["Aliases"].append({"Name": "unexpected"}),
    lambda responses: responses[("cloudfront", "get-distribution")]["Distribution"].update(Status="InProgress"),
    lambda responses: responses[("cloudfront", "get-distribution")]["Distribution"]["DistributionConfig"]["Aliases"].update(Items=["foreign.example.org"]),
    lambda responses: responses[("cloudfront", "get-distribution")]["Distribution"]["DistributionConfig"]["Origins"]["Items"][0].update(DomainName="other.example.org"),
    lambda responses: responses[("cloudfront", "get-distribution")]["Distribution"]["DistributionConfig"]["Origins"]["Items"].append(
        {"Id": "launcher", "DomainName": "secret.lambda-url.us-east-1.on.aws"}),
    lambda responses: responses[("cloudfront", "get-distribution")]["Distribution"]["DistributionConfig"]["CacheBehaviors"]["Items"].insert(
        0, {"PathPattern": "*/browser-launch", "TargetOriginId": "frontend"}),
    lambda responses: responses[("cloudfront", "get-distribution")]["Distribution"]["DistributionConfig"]["CacheBehaviors"]["Items"].append(
        {"PathPattern": "*/browser-launch", "TargetOriginId": "frontend"}),
])
def test_rejects_wrong_identity_url_public_behavior_unexpected_grant_or_alias(policy_case, tamper):
    manifest, path, directory, release_reader, responses = policy_case
    tamper(responses)
    with pytest.raises(ValueError):
        read_ingress_policy(manifest, deadline=time.monotonic() + 10, max_requests=20,
                            read=policy_read(responses, []))
    assert list(directory.iterdir()) == []


def test_approved_exact_helper_role_grant_and_denies_wildcard(policy_case):
    manifest, path, directory, reader, responses = policy_case
    grant = {"Effect": "Allow", "Principal": {"AWS": manifest["ingress_policy"]["role_arn"]},
             "Action": "lambda:InvokeFunction"}
    manifest["ingress_policy"]["allowed_invoke_statements"]["example-sso"] = [grant]
    responses[("lambda", "get-policy")] = {"Policy": json.dumps({"Statement": [grant]})}
    responses[("lambda", "get-policy", ("--function-name", "example-sso", "--qualifier", "active"))] = None
    assert read_ingress_policy(manifest, deadline=time.monotonic() + 10, max_requests=20,
                               read=policy_read(responses, []))["function_urls"] == []
    grant["Principal"] = "*"
    with pytest.raises(ValueError, match="grant"):
        read_ingress_policy(manifest, deadline=time.monotonic() + 10, max_requests=20,
                            read=policy_read(responses, []))


def test_missing_or_unapproved_policy_cannot_pass_selector(policy_case):
    manifest, path, directory, release_reader, responses = policy_case
    manifest["ingress_policy"].pop("functions")
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="scope"):
        qualify_journeys(manifest["release"]["source_sha"], path, directory,
                         reader_factory=lambda fixture, **bounds: FakeReader(fixture, release_reader, **bounds),
                         now=NOW)
    assert list(directory.iterdir()) == []


def test_request_bound_fails_closed(policy_case):
    manifest, path, directory, release_reader, responses = policy_case
    with pytest.raises(ValueError, match="bound"):
        read_ingress_policy(manifest, deadline=time.monotonic() + 10, max_requests=2,
                            read=policy_read(responses, []))


def test_scope_and_broker_identity_fail_closed(policy_case, monkeypatch):
    manifest, path, directory, reader, responses = policy_case
    manifest["ingress_policy"]["functions"] = [{}]
    with pytest.raises(ValueError, match="scope"):
        validate_policy_scope(manifest)
    manifest["ingress_policy"]["functions"] = ["example-sso"]
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        return type("Result", (), {"returncode": 0, "stdout": b'{}', "stderr": b''})()
    monkeypatch.setattr("ingress_policy.subprocess.run", run)
    assert aws_read(manifest["ingress_policy"], "sts", "get-caller-identity",
                    deadline=time.monotonic() + 10) == {}
    assert commands[0][:10] == ["adp-cred", "assume", "--service", "aws", "--label",
                                "example", "--exec", "aws", "sts", "get-caller-identity"]
    with pytest.raises(ValueError, match="deadline"):
        aws_read(manifest["ingress_policy"], "sts", "get-caller-identity",
                 deadline=time.monotonic() - 1)
