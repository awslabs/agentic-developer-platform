"""Fail-closed case inventory for a shared Product regression attempt."""

from __future__ import annotations

from tests.e2e.product_onboarding.product_completion_inventory import CASES, DEFERRED_ISSUES

ISSUES = (8250, 8252, 8256, 8258, 8261, 8264, 8267, 8268, 8273, 8276)


def suite_namespace(suite, prefix=None):
    import re

    name = f"{prefix}-{suite}" if prefix else suite
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", name):
        raise ValueError("Regression suite needs a bounded fixture namespace")
    return name


def required_cases(inventory=CASES):
    if set(inventory) != set(ISSUES):
        raise ValueError(
            "Original verification issue inventory is incomplete or unexpected"
        )
    cases = {}
    for issue in ISSUES:
        criteria = inventory[issue]
        expected = {f"AC-{number:02}" for number in range(1, 5 if issue == 8276 else 4)}
        if set(criteria) != expected:
            raise ValueError(
                f"Issue {issue} has missing or unexpected functional cases"
            )
        if issue in DEFERRED_ISSUES:
            continue
        for criterion, spec in criteria.items():
            check_id = f"issue-{issue}-{criterion.lower()}"
            if not spec.fixture_owner or not spec.evidence_builder:
                raise ValueError(f"{check_id} has no fixture owner or evidence builder")
            if not spec.producers:
                raise ValueError(
                    f"{check_id} has no deployed test producer; offline and report-only checks cannot substitute"
                )
            if len(set(spec.producers)) != len(spec.producers):
                raise ValueError(f"{check_id} has duplicate test producers")
            for selector in spec.producers:
                if (
                    not isinstance(selector, str)
                    or "collect_" in selector
                    or "not_run" in selector
                    or "--collect-only" in selector
                    or selector.startswith(("tests/unit/", "modules/"))
                    or selector.startswith("tests/e2e/product_onboarding/test_product_completion_")
                    or any(
                        f"test_v{version}.py::live_" in selector
                        for version in ("04", "05", "06", "07")
                    )
                    or not (selector.startswith("case:PR04-C0") or "::" in selector)
                ):
                    raise ValueError(f"{check_id} has a nonexecuting producer")
            cases[check_id] = spec
    return cases


def _pytest_results(
    selectors,
    *,
    execute,
    evidence_dir,
    working_directory=None,
    environment_overrides=None,
):
    import hashlib
    import json
    import os
    import subprocess
    import sys
    import tempfile
    from contextlib import nullcontext
    from pathlib import Path
    from xml.etree import ElementTree

    execute = execute or subprocess.run
    context = (
        nullcontext(evidence_dir)
        if evidence_dir is not None
        else tempfile.TemporaryDirectory(prefix="product-regression-")
    )
    with context as directory:
        report = Path(directory) / (
            "junit-"
            + hashlib.sha256("\0".join(selectors).encode()).hexdigest()[:20]
            + ".xml"
        )
        if report.exists() or report.is_symlink():
            raise ValueError("Attempt evidence already has a selected case report")
        if evidence_dir is not None and any(
            path.exists() or path.is_symlink()
            for path in (report.with_suffix(suffix) for suffix in (".stdout.txt", ".stderr.txt", ".execution.json"))
        ):
            raise FileExistsError("Attempt evidence already has selected execution diagnostics")
        root = Path(__file__).resolve().parents[3]
        selected = (
            tuple(
                str(root / selector.split("::", 1)[0])
                + "::"
                + selector.split("::", 1)[1]
                for selector in selectors
            )
            if working_directory
            else selectors
        )
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            f"--rootdir={root}",
            f"--junitxml={report}",
            *selected,
        ]
        options = {}
        if working_directory:
            results_root = Path(working_directory) / "test-results"
            results_root.mkdir(mode=0o700, exist_ok=True)
            screenshots = results_root / "screenshots"
            screenshots.mkdir(mode=0o700)
            options["env"] = {
                **os.environ,
                "PYTHONPATH": str(root) + os.pathsep + os.environ.get("PYTHONPATH", ""),
                "E2E_SCREENSHOT_DIR": str(screenshots),
                **(environment_overrides or {}),
            }
        def retain_diagnostics(stdout, stderr, *, returncode=None, timed_out=False):
            if evidence_dir is None:
                return
            files = {}
            for name, value in (("stdout", stdout), ("stderr", stderr)):
                content = value if isinstance(value, bytes) else (value or "").encode()
                path = report.with_suffix("." + name + ".txt")
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    output.write(content[:1024 * 1024])
                files[name] = {"path": path.name, "bytes": min(len(content), 1024 * 1024),
                               "truncated": len(content) > 1024 * 1024}
            path = report.with_suffix(".execution.json")
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "w") as output:
                json.dump({"command": command, "returncode": returncode, "timed_out": timed_out,
                           "outputs": files}, output, sort_keys=True)

        try:
            completed = execute(
                command,
                cwd=working_directory or root,
                text=True,
                capture_output=True,
                timeout=3600,
                **options,
            )
        except subprocess.TimeoutExpired as error:
            retain_diagnostics(error.stdout, error.stderr, timed_out=True)
            if report.is_file() and not report.is_symlink() and report.stat().st_uid == os.getuid():
                report.chmod(0o600)
            raise ValueError("Selected live case timed out; retained private execution diagnostics") from None
        retain_diagnostics(completed.stdout, completed.stderr, returncode=completed.returncode)
        if report.is_file() and not report.is_symlink() and report.stat().st_uid == os.getuid():
            report.chmod(0o600)
        if (
            completed.returncode != 0
            or not report.is_file()
            or report.is_symlink()
            or report.stat().st_uid != os.getuid()
            or report.stat().st_size > 1024 * 1024
        ):
            raise ValueError(
                "Selected live case failed or produced no bounded test report; inspect private execution diagnostics"
            )
        report.chmod(0o600)
        try:
            suite = ElementTree.parse(report).getroot()
            suites = [suite] if suite.tag == "testsuite" else list(suite)
            if not suites or any(item.tag != "testsuite" for item in suites):
                raise ValueError("Selected live case has invalid JUnit evidence")
            tests = sum(int(item.attrib["tests"]) for item in suites)
            failures = sum(
                int(item.attrib.get(field, "0"))
                for item in suites
                for field in ("failures", "errors", "skipped")
            )
            observed = [item for suite in suites for item in suite.findall("testcase")]
        except (ElementTree.ParseError, KeyError, TypeError, ValueError) as error:
            raise ValueError("Selected live case has invalid JUnit evidence") from error
        if (
            tests == 0
            or failures
            or len(observed) != tests
            or any(
                item.find(field) is not None
                for item in observed
                for field in ("failure", "error", "skipped")
            )
        ):
            raise ValueError("Selected live case was skipped, failed or not run")
        results = []
        matched = set()
        for selector in selectors:
            path, expected = selector.split("::", 1)
            expected_classname = path.removesuffix(".py").replace("/", ".")
            selected = [
                item
                for item in observed
                if item.get("classname") == expected_classname
                and (
                    item.get("name") == expected
                    or (
                        "[" not in expected
                        and item.get("name", "").startswith(expected + "[")
                    )
                )
            ]
            if not selected or any(id(item) in matched for item in selected):
                raise ValueError("Selected live case was skipped, failed or not run")
            matched.update(map(id, selected))
            result = {"selector": selector, "status": "passed", "tests": len(selected)}
            if evidence_dir is not None:
                result.update(
                    report_path=str(report),
                    report_sha256=hashlib.sha256(report.read_bytes()).hexdigest(),
                    command=command,
                )
            if working_directory is not None:
                result["suite_id"] = (environment_overrides or {}).get("PRODUCT_SUITE_ID")
                result["cleanup_path"] = str(Path(working_directory) / "test-results" / "product-suite-cleanup.json")
            results.append(result)
        if len(matched) != tests:
            raise ValueError("Selected live case was skipped, failed or not run")
        return results


def _cli_result(selector, cli_case):
    result = cli_case(selector)
    if (
        not isinstance(result, dict)
        or result.get("status") != "passed"
        or result.get("selector") != selector
        or not result.get("evidence_ref")
        or not result.get("run_id")
    ):
        raise ValueError("Installed CLI case did not produce verified run evidence")
    return result


def run_case(spec, *, cli_case=None, execute=None, evidence_dir=None, suite_id=None):
    import hashlib
    import os
    import stat
    from pathlib import Path

    if not spec.producers:
        raise ValueError("A report validator is not a deployed test producer")
    if any(
        selector.startswith("case:") for selector in spec.producers
    ) and not callable(cli_case):
        raise ValueError("An installed CLI case needs the owned evaluator adapter")
    if evidence_dir is not None:
        destination = Path(evidence_dir)
        if (
            not destination.is_absolute()
            or destination.is_symlink()
            or not destination.is_dir()
            or destination.stat().st_uid != os.getuid()
            or stat.S_IMODE(destination.stat().st_mode) != 0o700
        ):
            raise ValueError("Attempt evidence directory must be private and owned")
    results = []
    for selector in (*spec.producers, *((spec.validator,) if spec.validator else ())):
        if selector.startswith("case:"):
            results.append(_cli_result(selector, cli_case))
            continue
        options = {}
        if suite_id is not None:
            digest = hashlib.sha256(selector.encode()).hexdigest()[:8]
            work = destination / ("selected-" + digest)
            work.mkdir(mode=0o700)
            options = {"working_directory": work, "environment_overrides": {
                "PRODUCT_B03_MODE": "false", "PRODUCT_SUITE_ID": suite_namespace(digest, suite_id),
            }}
        results.extend(_pytest_results((selector,), execute=execute, evidence_dir=evidence_dir, **options))
    return results


def run_scope_suite(cases, *, evidence_root, cli_case, execute, namespace_prefix=None):
    from tests.e2e.product_onboarding.product_v04_evidence import SURFACE_SELECTORS

    scope_ids = tuple(f"issue-8250-ac-{number:02}" for number in (1, 2, 3))
    if not callable(cli_case):
        raise TypeError(
            "B03 needs the owned installed CLI evaluator before claiming fixtures"
        )
    for criterion, check_id in zip(("AC-01", "AC-02", "AC-03"), scope_ids):
        if (
            cases[check_id].producers
            != tuple(dict.fromkeys(SURFACE_SELECTORS[criterion].values()))
            or cases[check_id].fixture_owner != "product_scope_fixture"
        ):
            raise ValueError("B03 browser suite requires its reviewed owned selectors")
    selectors = tuple(
        selector
        for check_id in scope_ids
        for selector in cases[check_id].producers
        if not selector.startswith("case:")
    )
    directory = evidence_root / scope_ids[0]
    directory.mkdir(mode=0o700)
    (evidence_root / "test-results").mkdir(mode=0o700)
    browser = _pytest_results(
        selectors,
        execute=execute,
        evidence_dir=directory,
        working_directory=evidence_root,
        environment_overrides={"PRODUCT_SUITE_ID": suite_namespace("issue-8250", namespace_prefix),
                               "PRODUCT_B03_MODE": "true"},
    )
    results = {}
    for index, check_id in enumerate(scope_ids):
        if index:
            (evidence_root / check_id).mkdir(mode=0o700)
        results[check_id] = browser[index * 2 : index * 2 + 2] + [
            {**_cli_result(cases[check_id].producers[-1], cli_case),
             "suite_id": suite_namespace("issue-8250", namespace_prefix),
             "cleanup_path": str(evidence_root / "test-results" / "product-suite-cleanup.json")}
        ]
    return results


def shared_browser_selection(cases, issue):
    if issue not in (8252, 8258, 8268):
        raise ValueError("Browser issue does not own a grouped suite")
    numbers = (1,) if issue == 8258 else (1, 2, 3)
    check_ids = tuple(f"issue-{issue}-ac-{number:02}" for number in numbers)
    for number, check_id in enumerate(check_ids, 1):
        expected = CASES[issue][f"AC-{number:02}"]
        selected = cases[check_id]
        if (
            selected.producers != expected.producers
            or selected.fixture_owner != expected.fixture_owner
            or selected.validator
        ):
            raise ValueError(
                f"{check_id} differs from the reviewed owned browser suite"
            )
    selectors = tuple(
        selector for check_id in check_ids for selector in cases[check_id].producers
    )
    if len(set(selectors)) != len(selectors):
        raise ValueError("Shared browser suite has duplicate selectors")
    return check_ids, selectors


def verify_fixture_providers(cases, *, available=None):
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    for issue in (8261, 8264, 8267):
        for number in (1, 2, 3):
            check_id = f"issue-{issue}-ac-{number:02}"
            for selector in cases[check_id].producers:
                filename, function = selector.split("::", 1)
                path = root / filename
                tree = ast.parse(path.read_text())
                selected = [
                    node
                    for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == function
                ]
                if len(selected) != 1:
                    raise ValueError(f"{check_id} has no executable live selector")
                required = {
                    call.args[0].value
                    for call in ast.walk(selected[0])
                    if isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "getfixturevalue"
                    and call.args
                    and isinstance(call.args[0], ast.Constant)
                    and isinstance(call.args[0].value, str)
                    and call.args[0].value.startswith("product_v")
                }
                if not required:
                    raise ValueError(f"{check_id} has no owned deployed fixture")
                providers = available
                if providers is None:
                    providers = set()
                    for directory in (path.parent, *path.parents):
                        fixture_file = directory / "conftest.py"
                        if fixture_file.is_file() and fixture_file.is_relative_to(root):
                            for node in ast.parse(fixture_file.read_text()).body:
                                if not isinstance(node, ast.FunctionDef):
                                    continue
                                for decorator in node.decorator_list:
                                    target = decorator.func if isinstance(decorator, ast.Call) else decorator
                                    if not (isinstance(target, ast.Attribute)
                                            and isinstance(target.value, ast.Name)
                                            and target.value.id == "pytest" and target.attr == "fixture"):
                                        continue
                                    names = [keyword.value for keyword in decorator.keywords
                                             if keyword.arg == "name"] if isinstance(decorator, ast.Call) else []
                                    if not names:
                                        providers.add(node.name)
                                    elif (len(names) == 1 and isinstance(names[0], ast.Constant)
                                          and isinstance(names[0].value, str)):
                                        providers.add(names[0].value)
                if not required <= providers:
                    raise ValueError(
                        f"{check_id} is missing owned fixture providers: "
                        + ", ".join(sorted(required - providers))
                    )


def run_shared_browser_suite(cases, issue, *, evidence_root, execute, namespace_prefix=None):
    check_ids, selectors = shared_browser_selection(cases, issue)
    attempt = evidence_root / f"issue-{issue}-browser"
    attempt.mkdir(mode=0o700)
    (attempt / "test-results").mkdir(mode=0o700)
    directory = evidence_root / check_ids[0]
    directory.mkdir(mode=0o700)
    browser = _pytest_results(
        selectors,
        execute=execute,
        evidence_dir=directory,
        working_directory=attempt,
        environment_overrides={"PRODUCT_B03_MODE": "false",
                               "PRODUCT_SUITE_ID": suite_namespace(f"issue-{issue}", namespace_prefix)},
    )
    results = {}
    offset = 0
    for check_id in check_ids:
        if check_id != check_ids[0]:
            (evidence_root / check_id).mkdir(mode=0o700)
        count = len(cases[check_id].producers)
        results[check_id] = browser[offset : offset + count]
        offset += count
    return results


def catalog_selection(cases):
    check_ids = tuple(f"issue-8273-ac-{number:02}" for number in (1, 2, 3))
    for number, check_id in enumerate(check_ids, 1):
        if cases[check_id] != CASES[8273][f"AC-{number:02}"]:
            raise ValueError(f"{check_id} differs from the owned Catalog selectors")
    selectors = tuple(selector for check_id in check_ids for selector in cases[check_id].producers)
    if len(set(selectors)) != len(selectors):
        raise ValueError("Catalog suite has duplicate selectors")
    return check_ids, selectors


def run_catalog_suite(cases, *, evidence_root, execute, namespace_prefix=None):
    check_ids, selectors = catalog_selection(cases)
    attempt = evidence_root / "issue-8273-catalog"
    attempt.mkdir(mode=0o700)
    (attempt / "test-results").mkdir(mode=0o700)
    directory = evidence_root / check_ids[0]
    directory.mkdir(mode=0o700)
    observations = _pytest_results(
        selectors, execute=execute, evidence_dir=directory,
        working_directory=attempt, environment_overrides={"PRODUCT_B03_MODE": "false",
            "PRODUCT_SUITE_ID": suite_namespace("issue-8273", namespace_prefix)},
    )
    results = {}
    offset = 0
    for check_id in check_ids:
        if check_id != check_ids[0]:
            (evidence_root / check_id).mkdir(mode=0o700)
        count = len(cases[check_id].producers)
        results[check_id] = observations[offset:offset + count]
        offset += count
    return results


def run_inventory(evidence_root, *, inventory=CASES, cli_case=None, execute=None, namespace_prefix=None):
    import os
    import stat
    from pathlib import Path

    from tests.e2e.product_onboarding.product_completion_cli import scope_cli_evaluator

    cases = required_cases(inventory)
    root = Path(evidence_root)
    if cli_case is None:
        cli_case = scope_cli_evaluator(root, os.environ)
    if not callable(cli_case):
        raise TypeError(
            "The shared Product inventory needs the owned installed CLI evaluator"
        )
    if (
        not root.is_absolute()
        or root.is_symlink()
        or not root.is_dir()
        or root.stat().st_uid != os.getuid()
        or stat.S_IMODE(root.stat().st_mode) != 0o700
    ):
        raise ValueError("The attempt evidence root must be private and owned")
    scope_ids = {f"issue-8250-ac-{number:02}" for number in (1, 2, 3)}
    if any(cases[check_id].validator for check_id in scope_ids):
        raise ValueError(
            "B03 scope report validators require explicit suite integration"
        )
    for issue in (8252, 8258, 8268):
        shared_browser_selection(cases, issue)
    catalog_selection(cases)
    verify_fixture_providers(cases)
    catalog_ids = {f"issue-8273-ac-{number:02}" for number in (1, 2, 3)}
    browser_ids = {
        f"issue-{issue}-ac-{number:02}"
        for issue in (8252, 8258, 8268)
        for number in ((1,) if issue == 8258 else (1, 2, 3))
    }
    results = {}
    for check_id, spec in cases.items():
        if check_id in catalog_ids:
            if check_id == "issue-8273-ac-01":
                results.update(run_catalog_suite(cases, evidence_root=root, execute=execute, namespace_prefix=namespace_prefix))
            continue
        if check_id in scope_ids:
            if check_id == "issue-8250-ac-01":
                results.update(
                    run_scope_suite(
                        cases, evidence_root=root, cli_case=cli_case, execute=execute, namespace_prefix=namespace_prefix
                    )
                )
            continue
        if check_id in browser_ids:
            if check_id in ("issue-8252-ac-01", "issue-8258-ac-01", "issue-8268-ac-01"):
                issue = int(check_id.split("-")[1])
                results.update(
                    run_shared_browser_suite(
                        cases, issue, evidence_root=root, execute=execute, namespace_prefix=namespace_prefix
                    )
                )
            continue
        directory = root / check_id
        directory.mkdir(mode=0o700)
        results[check_id] = run_case(
            spec, cli_case=cli_case, execute=execute, evidence_dir=directory,
            suite_id=suite_namespace(check_id, namespace_prefix),
        )
    return results
