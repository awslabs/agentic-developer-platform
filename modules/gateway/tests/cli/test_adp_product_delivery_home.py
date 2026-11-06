"""Recorded Product CLI HTTP exchange for an explicit delivery home."""

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "cli/adp-product.py"
spec = importlib.util.spec_from_file_location("adp_product_delivery_home", SCRIPT)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def test_show_and_update_home_preserve_http_revision_contract(tmp_path, monkeypatch, capsys):
    home = {"provider": "github", "repository": "example/planning", "repository_id": 35}
    profile_path = "/v1/products/product-1/projects/project-1"
    initial = {"product_id": "product-1", "project_id": "project-1", "revision": 3, "delivery_home_repository": None, "delivery_home_implied": False}
    updated = {**initial, "revision": 4, "delivery_home_repository": home}
    recorded_http = [
        ("GET", profile_path, None, None, initial),
        (
            "PATCH",
            profile_path,
            {"delivery_home_repository": home},
            {"If-Profile-Revision": "3", "Idempotency-Key": "delivery_home_cli_12345"},
            updated,
        ),
        ("GET", profile_path, None, None, updated),
    ]

    class RecordedClient:
        def __init__(self):
            self.calls = []

        def request(self, method, path, body=None, **kwargs):
            self.calls.append((method, path, body, kwargs.get("extra_headers")))
            expected_method, expected_path, expected_body, expected_headers, response = recorded_http[len(self.calls) - 1]
            assert (method, path, body, kwargs.get("extra_headers")) == (expected_method, expected_path, expected_body, expected_headers)
            return response

    client = RecordedClient()
    monkeypatch.setattr(cli.common, "Api", lambda: client)
    monkeypatch.setattr(cli.common, "ensure_can_mutate", lambda *args, **kwargs: None)
    scope = ["--product", "product-1", "--project", "project-1"]
    assert cli.main(["show", *scope, "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["detail"]["delivery_home_repository"] is None
    patch = tmp_path / "delivery-home.json"
    patch.write_text(json.dumps({"delivery_home_repository": home}), encoding="utf-8")
    assert cli.main(["update", *scope, "--input", str(patch), "--revision", "3", "--idempotency-key", "delivery_home_cli_12345", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["detail"]["delivery_home_repository"] == home
    assert result["detail"]["revision"] == 4
    assert cli.main(["show", *scope, "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["detail"]["delivery_home_repository"] == home
    assert shown["detail"]["delivery_home_implied"] is False
    assert client.calls == [item[:4] for item in recorded_http]


def test_implied_home_can_be_made_explicit_then_cleared_with_revisioned_updates(tmp_path, monkeypatch, capsys):
    home = {"provider": "github", "repository": "example/planning", "repository_id": 35}
    path = "/v1/products/product-1/projects/project-1"
    implied = {"product_id": "product-1", "project_id": "project-1", "revision": 2, "delivery_home_repository": home, "delivery_home_implied": True}
    explicit = {**implied, "revision": 3, "delivery_home_implied": False}
    cleared = {**implied, "revision": 4}
    recorded_http = [
        ("GET", path, None, None, implied),
        ("PATCH", path, {"delivery_home_repository": home}, {"If-Profile-Revision": "2", "Idempotency-Key": "scenario_home_set_12345"}, explicit),
        ("GET", path, None, None, explicit),
        ("PATCH", path, {"delivery_home_repository": None}, {"If-Profile-Revision": "3", "Idempotency-Key": "scenario_home_clear_12345"}, cleared),
        ("GET", path, None, None, cleared),
    ]

    class RecordedClient:
        def __init__(self):
            self.calls = []

        def request(self, method, request_path, body=None, **kwargs):
            actual = (method, request_path, body, kwargs.get("extra_headers"))
            self.calls.append(actual)
            assert actual == recorded_http[len(self.calls) - 1][:4]
            return recorded_http[len(self.calls) - 1][4]

    client = RecordedClient()
    monkeypatch.setattr(cli.common, "Api", lambda: client)
    monkeypatch.setattr(cli.common, "ensure_can_mutate", lambda *args, **kwargs: None)
    scope = ["--product", "product-1", "--project", "project-1"]

    def invoke(command):
        assert cli.main([*command, "--json"]) == 0
        return json.loads(capsys.readouterr().out)["detail"]

    first = invoke(["show", *scope])
    assert first["delivery_home_repository"] == home and first["delivery_home_implied"] is True
    set_input = tmp_path / "set-home.json"
    set_input.write_text(json.dumps({"delivery_home_repository": home}), encoding="utf-8")
    saved = invoke(["update", *scope, "--input", str(set_input), "--revision", "2", "--idempotency-key", "scenario_home_set_12345"])
    assert saved["revision"] == 3 and saved["delivery_home_implied"] is False
    assert invoke(["show", *scope])["delivery_home_repository"] == home
    clear_input = tmp_path / "clear-home.json"
    clear_input.write_text(json.dumps({"delivery_home_repository": None}), encoding="utf-8")
    cleared_result = invoke(["update", *scope, "--input", str(clear_input), "--revision", "3", "--idempotency-key", "scenario_home_clear_12345"])
    assert cleared_result["revision"] == 4 and cleared_result["delivery_home_implied"] is True
    after = invoke(["show", *scope])
    assert after["delivery_home_repository"] == home and after["delivery_home_implied"] is True
    assert client.calls == [item[:4] for item in recorded_http]
