"""Dependency version constraints that protect the gateway image from runtime crashes.

Issue #6112: anyio must be >=4.14.2 (CVE fixes) but <4.15.0 (sentinel import
crash under the OTel operator's shadow typing_extensions). This test codifies
the constraint so a future `pyproject.toml` bump is caught before it ships.
"""

from importlib.metadata import version as pkg_version

from packaging.version import Version


def test_anyio_version_within_safe_range():
    """anyio must be >=4.14.2 (CVE-2026-63374/64847 fixes) and <4.15.0.

    4.15.0+ imports typing_extensions.sentinel, which is absent from the OTel
    auto-instrumentation operator's vendored typing_extensions, crash-looping
    gateway pods at import time (outage 2026-09-07).
    """
    v = Version(pkg_version("anyio"))
    assert v >= Version("4.14.2"), f"anyio {v} is below the CVE fix floor (4.14.2)"
    assert v < Version("4.15.0"), f"anyio {v} uses typing_extensions.sentinel which crashes under the OTel operator's shadow typing_extensions"


def test_anyio_does_not_import_sentinel():
    """Direct verification that the installed anyio has no sentinel import.

    Complements the version check: if a future 4.14.x patch somehow adds the
    import, this catches it regardless of version numbering.
    """
    import importlib
    import inspect

    import anyio

    # Inspect the top-level anyio package and its _core subpackage
    modules_to_check = [anyio]
    try:
        core = importlib.import_module("anyio._core._typedattr")
        modules_to_check.append(core)
    except ImportError:
        pass

    for mod in modules_to_check:
        source = inspect.getsource(mod)
        assert "from typing_extensions import sentinel" not in source, (
            f"{mod.__name__} imports typing_extensions.sentinel — unsafe under the OTel operator"
        )
