"""Deterministic file sharding before pytest imports test modules.

Every discovered file is assigned, including files without recorded durations.
Ordinary pytest marker/ignore rules still apply. File order and xdist loadfile
semantics are retained; the CLI directory stays together for its ordering needs.
"""

import fnmatch
import json
import math
from collections import defaultdict
from pathlib import Path

import pytest


def discover_files(root, arguments, patterns, norecursedirs, ignore=(), ignore_glob=()):
    files = set()
    ignored = {(root / path).resolve() for path in ignore}
    for argument in arguments:
        path = (root / argument.split("::", 1)[0]).resolve()
        candidates = [path] if path.is_file() else path.rglob("*.py")
        for candidate in candidates:
            relative = candidate.relative_to(root)
            if not any(fnmatch.fnmatch(candidate.name, pattern) for pattern in patterns):
                continue
            if any(any(fnmatch.fnmatch(part, pattern) for pattern in norecursedirs) for part in relative.parts[:-1]):
                continue
            if any(candidate == item or item in candidate.parents for item in ignored):
                continue
            if any(fnmatch.fnmatch(relative.as_posix(), pattern) or fnmatch.fnmatch(str(candidate), pattern) for pattern in ignore_glob):
                continue
            files.add(relative.as_posix())
    return sorted(files)


def partition_files(files, durations, count):
    """Largest estimated file first; deterministic ties, no omitted new files."""
    weights = defaultdict(float)
    for nodeid, duration in durations.items():
        if not isinstance(duration, int | float) or not math.isfinite(duration) or duration < 0:
            raise ValueError(f"Invalid duration for {nodeid}")
        weights[nodeid.split("::", 1)[0]] += duration
    known = [weights[file] for file in files if weights[file] > 0]
    fallback = sum(known) / len(known) if known else 1.0
    units = defaultdict(list)
    for file in files:
        units["tests/cli" if file.startswith("tests/cli/") else file].append(file)
    weighted = [(sum(weights[file] or fallback for file in members), key, members) for key, members in units.items()]
    shards = [[] for _ in range(count)]
    totals = [0.0] * count
    for weight, key, members in sorted(weighted, key=lambda item: (-item[0], item[1])):
        index = min(range(count), key=lambda index: (totals[index], index))
        shards[index].extend(members)
        totals[index] += weight
    return [sorted(shard) for shard in shards]


def pytest_addoption(parser):
    group = parser.getgroup("Gateway CI")
    group.addoption("--ci-file-shards", type=int, help="Partition test files before collection")
    group.addoption("--ci-file-shard", type=int, help="One-based file shard to execute")
    group.addoption("--ci-timing-input", default=".test_durations", help="Historical per-test timing JSON")
    group.addoption("--ci-timing-output", help="Write this run's per-test timings (including setup/teardown)")
    group.addoption("--ci-collection-output", help="Write selected test IDs for coverage/partition verification")


def pytest_configure(config):
    count, shard = config.getoption("ci_file_shards"), config.getoption("ci_file_shard")
    if (count is None) != (shard is None) or (count is not None and not 1 <= shard <= count):
        raise pytest.UsageError("--ci-file-shards and --ci-file-shard require 1 <= shard <= shards")
    if count is not None and config.getoption("splits", default=None):
        raise pytest.UsageError("Use file sharding without pytest-split's --splits")
    config.pluginmanager.register(CISelection(config, count, shard), "gateway-ci-selection")


class CISelection:
    def __init__(self, config, count, shard):
        self.config = config
        self.root = config.rootpath
        self.selected = None
        self.timings = defaultdict(float)
        self.nodeids = None
        if count is not None:
            files = discover_files(
                self.root,
                config.args,
                config.getini("python_files"),
                config.getini("norecursedirs"),
                config.getoption("ignore") or (),
                config.getoption("ignore_glob") or (),
            )
            timing_file = self.root / config.getoption("ci_timing_input")
            durations = json.loads(timing_file.read_text()) if timing_file.exists() else {}
            if not isinstance(durations, dict):
                raise pytest.UsageError("CI timing input must be a JSON object")
            try:
                shards = partition_files(files, durations, count)
            except ValueError as error:
                raise pytest.UsageError(str(error)) from error
            self.selected = set(shards[shard - 1])
            if not self.selected:
                raise pytest.UsageError(f"CI file shard {shard}/{count} is empty")

    def pytest_ignore_collect(self, collection_path, config):
        if self.selected is None or not collection_path.is_file():
            return None
        relative = collection_path.relative_to(self.root).as_posix()
        if any(fnmatch.fnmatch(collection_path.name, pattern) for pattern in config.getini("python_files")):
            return relative not in self.selected or None
        return None

    def pytest_pycollect_makemodule(self, module_path, parent):
        # pytest deliberately bypasses ignore_collect for an explicit file
        # argument. Keep the full argument set for identical xdist planning,
        # but don't import/collect an explicit file owned by another shard.
        if self.selected is not None and module_path.relative_to(self.root).as_posix() not in self.selected:
            return UnassignedModule.from_parent(parent, path=module_path)
        return None

    def pytest_collection_finish(self, session):
        self.nodeids = [item.nodeid for item in session.items]

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node, ids):
        # xdist verifies worker collections match before scheduling tests.
        self.nodeids = list(ids)

    def pytest_runtest_logreport(self, report):
        if math.isfinite(report.duration) and report.duration >= 0:
            self.timings[report.nodeid] += report.duration

    def pytest_sessionfinish(self, session, exitstatus):
        if hasattr(self.config, "workerinput"):
            return
        for option, content in (("ci_timing_output", dict(self.timings)), ("ci_collection_output", self.nodeids)):
            destination = self.config.getoption(option)
            if destination and content is not None:
                path = Path(destination)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(content, sort_keys=True, indent=2) + "\n")


class UnassignedModule(pytest.Module):
    def collect(self):
        return []
