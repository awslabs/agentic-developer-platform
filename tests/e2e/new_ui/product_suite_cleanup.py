"""Retain observed cleanup for a live suite, including its original run identity."""

from pathlib import Path
import os
import re

from tests.e2e.new_ui.product_fixture_ports import Membership
from tests.e2e.product_onboarding.product_v02_record import private_json


def validate_cleanup(receipt, environment):
    run_id = f"product-{environment['GITHUB_RUN_ID']}-{environment['GITHUB_RUN_ATTEMPT']}"
    source = environment["PRODUCT_EXPECTED_REVISION"]
    suite = environment.get("PRODUCT_SUITE_ID") or None
    if (not re.fullmatch(r"product-[1-9][0-9]{0,19}-[1-9][0-9]{0,5}", run_id)
            or not re.fullmatch(r"[a-f0-9]{40}", source)
            or not isinstance(receipt, dict)
            or receipt.get("run_id") != run_id or receipt.get("source_revision") != source
            or receipt.get("suite_id") != suite):
        raise ValueError("Suite cleanup differs from the original workflow attempt")
    cases = receipt.get("cases")
    memberships = receipt.get("memberships")
    lock = receipt.get("ownership_lock")
    if (not isinstance(cases, dict) or not cases
            or not isinstance(memberships, dict) or "owner" not in memberships
            or not set(memberships) <= {"owner", "editor"}
            or not isinstance(lock, dict) or lock.get("released") is not True
            or not isinstance(lock.get("version"), str) or not lock["version"]):
        raise ValueError("Suite cleanup needs cases, membership observations and a released lock")
    for cleanup in cases.values():
        if (not isinstance(cleanup, dict) or cleanup.get("run_id") != run_id
                or any(cleanup.get(key) is not True for key in
                       ("fixture_removed", "owned_absent", "shared_unchanged"))
                or not cleanup.get("product_id") or not cleanup.get("project_id")):
            raise ValueError("Suite cleanup lacks an owned retirement readback")
    for membership in memberships.values():
        if not isinstance(membership, dict):
            raise ValueError("Suite cleanup lacks a membership readback")
        before, after = membership.get("baseline"), membership.get("observed")
        if (not isinstance(before, dict) or not isinstance(after, dict)
                or not isinstance(before.get("resource"), dict)
                or not isinstance(after.get("resource"), dict)
                or any(after.get(key) != before.get(key) for key in ("id", "org_id", "kind"))
                or after["resource"].get("membership_status") != "active"
                or Membership.shape(before) != Membership.shape(after)):
            raise ValueError("Suite cleanup membership differs from its baseline")
    return receipt


def retain_cleanup(receipt, directory, environment):
    validate_cleanup(receipt, environment)
    root = Path(directory)
    if (not root.is_absolute() or root.is_symlink() or not root.is_dir()
            or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077):
        raise ValueError("Suite cleanup directory must be private and owned")
    return private_json(root / "product-suite-cleanup.json", receipt)
