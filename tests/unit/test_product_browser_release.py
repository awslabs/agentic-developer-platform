"""Offline release integrity and viewport evidence checks for Product UI."""

import hashlib
import os
import stat

import pytest

from tests.e2e.new_ui.product_release import (
    PRODUCT_CHUNKS,
    capture_product_viewports,
    check_served_assets,
    private_capture_directory,
)

SOURCE = "a" * 40
BASE = "https://example.invalid"


class Response:
    status = 200

    def __init__(self, body):
        self.content = body

    def body(self):
        return self.content


class Request:
    def __init__(self, files):
        self.files = files
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return Response(self.files[url])


@pytest.fixture
def released_assets():
    files = {
        BASE + "/index.html": b'<script type="module" src="/assets/index-build.js"></script>',
        BASE + "/runtime-config.js": b'window.__RUNTIME_CONFIG__={};',
        BASE + "/assets/index-build.js": b"main bundle",
    }
    for chunk in PRODUCT_CHUNKS:
        files[BASE + "/assets/" + chunk + "-build.js"] = chunk.encode()
    files[BASE + "/products"] = files[BASE + "/index.html"]
    assets = {url.removeprefix(BASE + "/"): hashlib.sha256(body).hexdigest()
              for url, body in files.items() if url != BASE + "/products"}
    receipt = {"verified": True, "domain": "example.invalid", "release": {
        "component": "gateway-frontend", "source_revision": SOURCE,
        "workflow_path": ".github/workflows/gateway-frontend-deploy.yml", "assets": assets,
    }}
    return files, receipt


def test_served_product_assets_match_verified_release(released_assets):
    files, receipt = released_assets
    request = Request(files)
    result = check_served_assets(request, BASE, receipt, SOURCE)
    assert result == {"source_revision": SOURCE, "deployed_source_revision": SOURCE,
                      "asset_count": 8, "status": "matched"}
    assert request.calls[-1] == BASE + "/products"


@pytest.mark.parametrize("failure", ["wrong_bundle", "wrong_domain", "wrong_source", "unverified", "missing_chunk", "route_mismatch"])
def test_wrong_release_refused_before_browser_mutation(released_assets, failure):
    files, receipt = released_assets
    request = Request(dict(files))
    if failure == "wrong_bundle":
        request.files[BASE + "/assets/ProductCreate-build.js"] = b"wrong bundle"
    elif failure == "wrong_domain":
        receipt["domain"] = "other.invalid"
    elif failure == "wrong_source":
        receipt["release"]["source_revision"] = "b" * 40
    elif failure == "unverified":
        receipt["verified"] = False
    elif failure == "missing_chunk":
        receipt["release"]["assets"].pop("assets/ProductCreate-build.js")
    else:
        request.files[BASE + "/products"] = b"other index"
    with pytest.raises(AssertionError):
        check_served_assets(request, BASE, receipt, SOURCE)
    assert all("/api/v1/products" not in call for call in request.calls)


class Page:
    def __init__(self, overflow=False, keyboard="product-edit-purpose"):
        self.viewport = []
        self.overflow = overflow
        self.keyboard_value = keyboard
        self.keyboard = self
        self.clicked = []
        self.selected_role = None

    def set_viewport_size(self, viewport):
        self.viewport.append(viewport["width"])

    def get_by_role(self, *args, **kwargs):
        self.selected_role = (args[0], kwargs.get("name"))
        if args == ("textbox",):
            assert kwargs == {"name": "Name", "exact": True}
        return self

    def click(self):
        self.clicked.append(self.selected_role)

    def focus(self):
        pass

    def press(self, key):
        assert key == "Tab"

    def evaluate(self, script):
        return not self.overflow if "scrollWidth" in script else self.keyboard_value

    def screenshot(self, **kwargs):
        return b"private synthetic image"


class Expectation:
    def to_be_visible(self):
        pass


def test_viewports_have_private_images_and_keyboard_order(tmp_path, monkeypatch):
    import playwright.sync_api

    monkeypatch.setattr(playwright.sync_api, "expect", lambda _: Expectation())
    directory = private_capture_directory(str(tmp_path / "private"))
    page = Page()
    result = capture_product_viewports(page, directory, "owned-run", "Owned product")
    assert page.viewport == [390, 1440]
    assert [name.pattern if hasattr(name, "pattern") else name for _, name in page.clicked] == [
        "Profile setup", "Product information", "Profile setup", "Product information",
    ]
    assert [entry["width"] for entry in result] == [390, 1440]
    for entry in result:
        assert stat.S_IMODE(os.stat(entry["screenshot"]).st_mode) == 0o600
    with pytest.raises(FileExistsError):
        capture_product_viewports(page, directory, "owned-run", "Owned product")


@pytest.mark.parametrize("problem", [{"overflow": True}, {"keyboard": "wrong-field"}])
def test_viewport_failure_cannot_produce_success(tmp_path, monkeypatch, problem):
    import playwright.sync_api

    monkeypatch.setattr(playwright.sync_api, "expect", lambda _: Expectation())
    directory = private_capture_directory(str(tmp_path / "private"))
    with pytest.raises(AssertionError):
        capture_product_viewports(Page(**problem), directory, "owned-run", "Owned product")


def test_asset_preflight_has_a_total_deadline(released_assets, monkeypatch):
    from tests.e2e.new_ui import product_release

    clock = iter((0.0, 0.0, 31.0))
    monkeypatch.setattr(product_release.time, "monotonic", lambda: next(clock))
    files, receipt = released_assets
    request = Request(files)
    with pytest.raises(AssertionError, match="timed out"):
        check_served_assets(request, BASE, receipt, SOURCE)
    assert len(request.calls) == 1


def test_viewports_capture_separate_private_traces(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import playwright.sync_api

    class Tracing:
        def __init__(self):
            self.starts = 0
            self.paths = []

        def start(self, **options):
            assert options == {"screenshots": True, "snapshots": True}
            self.starts += 1

        def stop(self, *, path):
            self.paths.append(path)
            with open(path, "wb") as output:
                output.write(b"synthetic trace")

    monkeypatch.setattr(playwright.sync_api, "expect", lambda _: Expectation())
    directory = private_capture_directory(str(tmp_path / "private"))
    page = Page()
    tracing = Tracing()
    page.context = SimpleNamespace(tracing=tracing)
    result = capture_product_viewports(page, directory, "owned-run", "Owned product", trace=True)
    assert tracing.starts == 2
    assert tracing.paths == [entry["trace"] for entry in result]
    assert [entry["width"] for entry in result] == [390, 1440]
    for entry in result:
        assert stat.S_IMODE(os.stat(entry["trace"]).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(entry["screenshot"]).st_mode) == 0o600
    with pytest.raises(AssertionError, match="already exists"):
        capture_product_viewports(page, directory, "owned-run", "Owned product", trace=True)
