"""Check the adapter observer inside the gateway build, using only stdlib.

Run with ``python -I -B -S src/orchestration/adapter_observer_selfcheck.py``.
The isolated interpreter cannot resolve checkout packages or site dependencies.
Staging records the canonical owner's source hash; this check imports those
exact bytes and exercises the entry point without any cluster or credentials.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path


def check() -> None:
    directory = Path(__file__).resolve().parents[2] / "contracts/tool-adapters"
    source = directory / "runtime_evidence.py"
    digest = directory / "runtime_evidence.sha256"
    if not directory.is_dir() or {path.name for path in directory.iterdir()} != {source.name, digest.name}:
        raise ValueError("observer staging must contain only its source and checksum")
    if any(path.is_symlink() or not path.is_file() for path in (source, digest)):
        raise ValueError("observer staging must contain regular files")
    if not 0 < source.stat().st_size <= 64 * 1024 or digest.stat().st_size > 128:
        raise ValueError("observer staging exceeds its size bound")
    expected = hashlib.sha256(source.read_bytes()).hexdigest() + "  runtime_evidence.py\n"
    if digest.read_text() != expected:
        raise ValueError("observer staging checksum mismatch")
    spec = importlib.util.spec_from_file_location("adp_adapter_observer_selfcheck", source)
    if spec is None or spec.loader is None:
        raise ValueError("observer staging cannot be loaded")
    observer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(observer)
    error = getattr(observer, "ObservationError", None)
    if not callable(getattr(observer, "observe", None)) or not isinstance(error, type) or not issubclass(error, Exception):
        raise ValueError("observer entry point is unavailable")

    def unexpected_read(_path):
        raise ValueError("observer read before validating expectations")

    try:
        observer.observe(unexpected_read, {}, {})
    except error as exc:
        if str(exc) != "adapter_expected_resources_invalid":
            raise ValueError("observer rejection contract changed") from None
    else:
        raise ValueError("observer accepted missing expectations")


def main() -> int:
    try:
        check()
    except Exception as exc:
        print(f"Adapter observer selfcheck failed: {exc}", file=sys.stderr)
        return 1
    print("Adapter observer selfcheck passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
