"""Selected API base provenance is identical in local and private-cluster paths.

Registry, Docker and HTTP transports are explicit doubles. Manifest/config hashes,
lock validation and installer decisions execute without cloud or cluster actions.
"""

import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from installation import runner
from installation.config import Refusal, validate

BASE = "registry.example.test/reviewed-python@sha256:" + "b" * 64


def result(value):
    return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr="")


def encoded(value):
    return json.dumps(value, sort_keys=True).encode()


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class ImageTools:
    def __init__(self, release, base, mutation, environment):
        self.calls, self.configs, self.manifests, self.tags = [], {}, {}, {}
        self.release, self.mutation = release, mutation
        self.environment = environment
        for name in (
            "superplane-api",
            "superplane-controller",
            "superplane-platform-monitor",
        ):
            revision = release["image_sources"][name]["source_revision"]
            labels = {"org.opencontainers.image.revision": revision}
            tag = revision
            if name == "superplane-api":
                if base:
                    labels["org.opencontainers.image.base.name"] = base
                    tag += "-py-" + base.rsplit(":", 1)[1]
                if mutation == "default_tag":
                    tag = revision
                elif mutation == "wrong_base_tag":
                    tag = revision + "-py-" + "c" * 64
                elif mutation == "truncated_tag":
                    tag = tag[:64]
                elif mutation == "wrong_revision":
                    labels["org.opencontainers.image.revision"] = "c" * 40
                elif mutation == "missing_base_label":
                    labels.pop("org.opencontainers.image.base.name", None)
                elif mutation == "wrong_base_label":
                    labels["org.opencontainers.image.base.name"] = base.replace(
                        "b" * 64, "c" * 64
                    )
                elif mutation == "wrong_base_repository":
                    labels["org.opencontainers.image.base.name"] = base.replace(
                        "reviewed-python", "unreviewed-python"
                    )
                elif mutation == "legacy_short_tag":
                    tag = revision[:12]
            config = encoded(
                {"architecture": "amd64", "os": "linux", "config": {"Labels": labels}}
            )
            manifest = encoded(
                {"schemaVersion": 2, "config": {"digest": digest(config)}}
            )
            self.configs[name], self.manifests[name], self.tags[name] = (
                config,
                manifest,
                [tag],
            )
            release["images"][name] = digest(manifest)

    def name(self, args):
        for name in self.configs:
            if "adp-" + name in args or any(
                self.release["images"][name] in arg for arg in args
            ):
                return name
        raise AssertionError(args)

    def call(self, args, **kwargs):
        self.calls.append(args)
        if "get-caller-identity" in args:
            return result(
                {
                    "Account": self.environment["account_id"],
                    "Arn": f"arn:aws:sts::{self.environment['account_id']}:assumed-role/test-installer/fixture",
                    "UserId": self.environment["deployment_identity"][
                        "expected_role_id"
                    ]
                    + ":fixture",
                }
            )
        if "get-role" in args:
            selected = self.environment["deployment_identity"]
            return result(
                {
                    "Role": {
                        "Arn": selected["expected_role_arn"],
                        "RoleId": selected["expected_role_id"],
                    }
                }
            )
        if "describe-images" in args:
            name = self.name(args)
            return result(
                {
                    "imageDetails": [
                        {
                            "imageDigest": self.release["images"][name],
                            "imageTags": self.tags[name],
                        }
                    ]
                }
            )
        if "batch-get-image" in args:
            name = self.name(args)
            raw = self.manifests[name].decode()
            if name == "superplane-api" and self.mutation == "manifest_digest":
                raw += " "
            return result({"images": [{"imageManifest": raw}]})
        if "get-download-url-for-layer" in args:
            return result(
                {"downloadUrl": "https://registry.example.test/" + self.name(args)}
            )
        if args[:3] == ["docker", "image", "inspect"]:
            config = json.loads(self.configs[self.name(args)])
            return result([{"Config": config["config"]}])
        if args[:2] == ["docker", "pull"]:
            return result({})
        if args[:2] == ["docker", "run"]:
            return result({"controller_management": True})
        raise AssertionError(args)

    def get(self, url, **kwargs):
        assert kwargs == {"timeout": 30, "follow_redirects": False}
        name = url.rsplit("/", 1)[1]
        raw = self.configs[name]
        if name == "superplane-api" and self.mutation == "config_digest":
            raw += b" "
        return httpx.Response(200, content=raw)


class Probe:
    def __init__(self, installer):
        self.installer = installer

    def __enter__(self):
        self.installer.probe_entered = True
        return self

    def __exit__(self, *args):
        pass

    def prove_network_policy(self):
        pass

    def isolate(self):
        pass

    def run(self, *args, **kwargs):
        return result({"controller_management": True})


@pytest.mark.parametrize("mode", ["local", "cluster"])
@pytest.mark.parametrize("selection", ["selected", "default", "legacy_short_tag"])
def test_selected_and_default_images_pass_both_paths(
    tmp_path, environment, release, monkeypatch, mode, selection
):
    environment.pop("execution")
    environment.pop("controller_profiles")
    if mode == "cluster":
        environment["image_execution"] = "cluster"
    base = BASE if selection == "selected" else None
    if base:
        release["image_sources"]["superplane-api"]["python_image"] = base
    tools = ImageTools(release, base, selection, environment)
    monkeypatch.setattr(runner.httpx, "get", tools.get)
    monkeypatch.setattr(runner, "ClusterProbe", Probe)
    installer = runner.Installer(
        environment, release, tmp_path, tools, control_plane_only=True
    )
    installer.images()
    assert sum("describe-images" in call for call in tools.calls) == 3
    assert (
        any(call[:2] == ["docker", "run"] for call in tools.calls)
        or installer.probe_entered
    )


@pytest.mark.parametrize("mode", ["local", "cluster"])
@pytest.mark.parametrize(
    "mutation",
    [
        "default_tag",
        "wrong_base_tag",
        "truncated_tag",
        "wrong_revision",
        "missing_base_label",
        "wrong_base_label",
        "wrong_base_repository",
    ],
)
def test_selected_base_substitution_refuses_before_runtime_probe(
    tmp_path, environment, release, monkeypatch, mode, mutation
):
    environment.pop("execution")
    environment.pop("controller_profiles")
    environment["image_execution"] = mode
    release["image_sources"]["superplane-api"]["python_image"] = BASE
    tools = ImageTools(release, BASE, mutation, environment)
    monkeypatch.setattr(runner.httpx, "get", tools.get)
    monkeypatch.setattr(runner, "ClusterProbe", Probe)
    installer = runner.Installer(
        environment, release, tmp_path, tools, control_plane_only=True
    )
    with pytest.raises(Refusal, match="bind image|OCI provenance"):
        installer.images()
    assert not getattr(installer, "probe_entered", False)
    assert not any(call[:2] == ["docker", "run"] for call in tools.calls)


@pytest.mark.parametrize("mutation", ["manifest_digest", "config_digest"])
def test_cluster_selected_base_preserves_raw_hash_checks(
    tmp_path, environment, release, monkeypatch, mutation
):
    environment.pop("execution")
    environment["image_execution"] = "cluster"
    release["image_sources"]["superplane-api"]["python_image"] = BASE
    tools = ImageTools(release, BASE, mutation, environment)
    monkeypatch.setattr(runner.httpx, "get", tools.get)
    monkeypatch.setattr(runner, "ClusterProbe", Probe)
    installer = runner.Installer(
        environment, release, tmp_path, tools, control_plane_only=True
    )
    with pytest.raises(Refusal, match="manifest does not match|config content digest"):
        installer.images()
    assert not getattr(installer, "probe_entered", False)


@pytest.mark.parametrize(
    "base",
    [
        None,
        "",
        [],
        12,
        "python:3.12",
        "repo@sha256:" + "0" * 64,
        "repo@sha256:" + "b" * 63,
        BASE + "\n",
        "x" * 2049 + "@sha256:" + "b" * 64,
    ],
)
def test_release_lock_refuses_nonexact_selected_base(environment, release, base):
    release["image_sources"]["superplane-api"]["python_image"] = base
    with pytest.raises(Refusal, match="Selected Python base"):
        validate(environment, release)


def test_release_lock_accepts_exact_api_base_and_refuses_other_component(
    environment, release
):
    release["image_sources"]["superplane-api"]["python_image"] = BASE
    validate(environment, release)
    release["image_sources"]["superplane-controller"]["python_image"] = BASE
    with pytest.raises(Refusal, match="Selected Python base"):
        validate(environment, release)
