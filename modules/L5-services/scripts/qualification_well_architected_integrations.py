"""Execute original automatic-integration criteria at the reviewed source.

The subprocess receives no deployment credentials and cannot produce installed
or live acceptance. Installed lifecycle and manual scenarios remain separate.
"""

from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "modules/L5-services"
SELECTORS = {
    "WA01/AC-02": ["test_wa01.py::" + name for name in (
        "test_ac_02", "test_ac_02_lost_start_and_local_expiry", "test_ac_02_reject_incomplete_readback", "test_scn_02")],
    "WA01/AC-03": ["test_wa01.py::" + name for name in (
        "test_ac_03", "test_ac_03_shared_l5_validation", "test_ac_03_all_service_variants_remain_disjoint",
        "test_ac_03_operation_parity_and_catalog_block", "test_scn_03")],
    "WA02/AC-01": ["test_wa02.py::test_ac_01"],
    "WA02/AC-04": ["test_wa02.py::test_ac_04"],
    "WA03/AC-01": ["test_wa03.py::test_ac_01"],
    "WA04/AC-04": ["test_wa04.py::test_ac_04"],
    "WA05/AC-01": ["test_wa05.py::test_ac_01"],
    "WA05/AC-04": ["test_wa05.py::test_ac_04"],
    "WA06/AC-03": ["test_wa06.py::test_ac_03"],
}


def selector_digest():
    return sha256(json.dumps(SELECTORS, sort_keys=True).encode()).hexdigest()


def verify_source(source, *, run=subprocess.run):
    revision = run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, timeout=15, check=True)
    dirty = run(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=ROOT,
                capture_output=True, timeout=15, check=True)
    if revision.stdout.decode().strip() != source or dirty.stdout.strip():
        raise ValueError("integration acceptance requires the exact clean reviewed Git source")


def evaluate_xml(content, source):
    if len(content) > 4 * 1024 * 1024 or b"<!DOCTYPE" in content or b"<!ENTITY" in content:
        raise ValueError("integration report exceeds its bounded XML contract")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as error:
        raise ValueError("integration report is not valid XML") from error
    cases = list(root.iter("testcase"))
    if not cases or len(cases) > 1000:
        raise ValueError("integration report has no bounded test cases")
    rows = []
    for criterion, selectors in SELECTORS.items():
        passed = True
        count = 0
        for selector in selectors:
            filename, test = selector.split("::")
            matches = [item for item in cases if item.get("classname", "").split(".")[-1] == filename[:-3]
                       and (item.get("name") == test or item.get("name", "").startswith(test + "["))]
            count += len(matches)
            if not matches or any(any(item.find(tag) is not None for tag in ("failure", "error", "skipped")) for item in matches):
                passed = False
        rows.append({"id": criterion, "outcome": "passed" if passed else "incomplete", "evidence_verified": passed,
                     "observation": "executed-integration", "verification_layer": "source-integration",
                     "evidence_ref": "integration-results.xml", "test_cases": count})
    return {"schema_version": 1, "source_revision": source, "selectors_sha256": selector_digest(),
            "junit_sha256": sha256(content).hexdigest(), "criteria": rows}


def read(directory, context, driver):
    report = driver.private_json(directory / "integration-observations.json")
    path = directory / "integration-results.xml"
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077 or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("integration results must remain private and bounded")
    expected = {"context": context, **evaluate_xml(path.read_bytes(), context["source_revision"])}
    if report != expected:
        raise ValueError("integration results differ from the exact reviewed source and selectors")
    return report


def collect(directory, context, driver, *, run=subprocess.run):
    verify_source(context["source_revision"], run=run)
    if (directory / "integration-observations.json").exists():
        return read(directory, context, driver)
    destination = driver.private_output(directory / "integration-results.xml")
    transcript = driver.private_output(directory / "integration-output.txt")
    environment = {key: value for key, value in os.environ.items()
                   if key in {"HOME", "PATH", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT"}}
    environment.update(PYTHONPATH=str(SERVICE / "src") + os.pathsep + str(ROOT / "modules/gateway"),
                       BG_TOKEN_SECRET_KEY="offline-integration-fixture-key", AWS_EC2_METADATA_DISABLED="true")
    selectors = ["tests/well_architected/" + selector for values in SELECTORS.values() for selector in values]
    # A private transcript retains actionable failures without leaking fixture
    # content into public workflow logs. pytest never receives provider tokens.
    descriptor = os.open(transcript, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        completed = run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--disable-warnings",
                         "--junitxml=" + str(destination), *selectors], cwd=SERVICE, env=environment,
                        stdout=output, stderr=subprocess.STDOUT, timeout=600, check=False)
    destination.chmod(0o600)
    report = {"context": context, **evaluate_xml(destination.read_bytes(), context["source_revision"])}
    if completed.returncode != 0:
        # Collection/import errors can coexist with passing test cases. Never
        # accept only that surviving subset as a completed source check.
        for row in report["criteria"]:
            row.update(outcome="incomplete", evidence_verified=False)
        raise ValueError("reviewed integration criteria failed; inspect private integration-output.txt")
    driver.write_private(directory / "integration-observations.json", report)
    return report
