"""Compare served Product assets with a separately verified frontend release."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

PRODUCT_CHUNKS = ("ProductCreate", "ProductOverview", "ProductsList", "productDraft", "products")


def check_served_assets(request, base_url, receipt, expected_source):
    origin = urlsplit(base_url)
    assert origin.scheme == "https" and origin.hostname and not origin.path.rstrip("/"), "Browser target must be a HTTPS origin"
    assert isinstance(receipt, dict) and receipt.get("verified") is True, "Frontend receipt must be authenticated by the maintained evidence store"
    release = receipt.get("release")
    assert isinstance(release, dict) and release.get("component") == "gateway-frontend", "Wrong frontend release component"
    assert re.fullmatch(r"[a-f0-9]{40}", expected_source), "Expected source revision is not pinned"
    from tests.e2e.product_onboarding.product_completion_observe import (
        compatible_source,
    )

    try:
        compatible_source(release.get("source_revision"), expected_source, "gateway-frontend")
    except ValueError as error:
        raise AssertionError("Frontend source differs from the selected release: " + str(error)) from error
    assert release.get("workflow_path") == ".github/workflows/gateway-frontend-deploy.yml", "Frontend workflow differs from the reviewed publisher"
    assert receipt.get("domain") == origin.hostname, "Served origin differs from the verified distribution"
    assets = release.get("assets")
    assert isinstance(assets, dict) and 7 <= len(assets) <= 256, "Frontend asset inventory is incomplete or unbounded"
    assert "index.html" in assets and "runtime-config.js" in assets, "Frontend entry or runtime configuration missing"
    for chunk in PRODUCT_CHUNKS:
        assert any(re.fullmatch(r"assets/" + chunk + r"-[A-Za-z0-9_-]+\.js", path) for path in assets), "Product route chunk missing"
    deadline = time.monotonic() + 30
    total = 0
    index = None
    for path, expected_hash in sorted(assets.items()):
        assert path and not path.startswith("/") and ".." not in path.split("/") and "\\" not in path, "Unsafe asset path"
        assert re.fullmatch(r"[a-f0-9]{64}", expected_hash), "Frontend asset digest missing"
        remaining = deadline - time.monotonic()
        assert remaining > 0, "Frontend release observation timed out"
        response = request.get(base_url + "/" + quote(path, safe="/"), timeout=min(int(remaining * 1000), 10_000))
        assert response.status == 200, f"Served asset unavailable: HTTP {response.status}"
        payload = response.body()
        total += len(payload)
        assert len(payload) <= 8 * 1024 * 1024 and total <= 32 * 1024 * 1024, "Served assets exceed release bounds"
        assert hashlib.sha256(payload).hexdigest() == expected_hash, "Wrong frontend bundle served"
        if path == "index.html":
            index = payload
    remaining = deadline - time.monotonic()
    assert remaining > 0, "Frontend release observation timed out"
    route = request.get(base_url + "/products", timeout=min(int(remaining * 1000), 10_000))
    assert route.status == 200 and hashlib.sha256(route.body()).hexdigest() == assets["index.html"], "Product route serves a different release"
    assert any(("/" + path).encode() in index for path in assets if path.startswith("assets/index-") and path.endswith(".js")), "Frontend entry has no pinned main bundle"
    return {"source_revision": expected_source, "deployed_source_revision": release["source_revision"],
            "asset_count": len(assets), "status": "matched"}


def private_capture_directory(path):
    directory = Path(path)
    assert directory.is_absolute(), "Private screenshot directory must be absolute"
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    mode = directory.stat()
    assert mode.st_uid == os.getuid() and stat.S_ISDIR(mode.st_mode) and stat.S_IMODE(mode.st_mode) == 0o700, "Screenshot directory is not private"
    return directory


def capture_product_viewports(page, directory, run_id, heading, *, trace=False):
    assert re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", run_id), "Invalid screenshot run identity"
    from playwright.sync_api import expect

    result = []
    for width in (390, 1440):
        trace_path = directory / f"{run_id}-{width}.zip"
        if trace:
            assert not trace_path.exists() and not trace_path.is_symlink(), "Product trace already exists for this run and viewport"
            page.context.tracing.start(screenshots=True, snapshots=True)
        try:
            page.set_viewport_size({"width": width, "height": 900})
            expect(page.get_by_role("heading", name=heading)).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Product workspace overflows viewport"
            from tests.e2e.new_ui.product_navigation import open_product_section

            open_product_section(page)
            page.get_by_role("textbox", name="Name", exact=True).focus()
            page.keyboard.press("Tab")
            assert page.evaluate("document.activeElement.id") == "product-edit-purpose", "Keyboard focus did not reach the next field"
            path = directory / f"{run_id}-{width}.png"
            image = page.screenshot(full_page=True)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(image)
            entry = {"width": width, "screenshot": str(path), "overflow": False, "keyboard": "pass"}
        finally:
            if trace:
                page.context.tracing.stop(path=str(trace_path))
                os.chmod(trace_path, 0o600)
        if trace:
            entry["trace"] = str(trace_path)
        result.append(entry)
    return result
