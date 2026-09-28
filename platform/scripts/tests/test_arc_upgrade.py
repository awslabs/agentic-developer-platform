import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location("arc_upgrade", Path(__file__).parents[1] / "check-arc-upgrade.py")
arc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(arc)


def plan(version="0.14.2", actions=None):
    return {"resource_changes": [{"type": "helm_release", "change": {"actions": actions or ["update"], "after": {"chart": "gha-runner-scale-set-controller", "version": version}}}]}


def scale_set(version):
    return {"metadata": {"name": "production-runners", "labels": {"app.kubernetes.io/version": version}}}


def test_in_place_helm_update_cannot_delete_old_minor_scale_sets():
    with pytest.raises(ValueError, match="would delete"):
        arc.verify(plan(), [scale_set("0.13.1")])


def test_matching_minor_and_security_suffix_are_compatible():
    arc.verify(plan(), [scale_set("0.14.1"), scale_set("0.14.2-adp.security.1")])


def test_missing_resource_version_fails_closed():
    with pytest.raises(ValueError, match="missing or malformed"):
        arc.verify(plan(), [scale_set(None)])


def test_unresolved_controller_version_fails_closed():
    with pytest.raises(ValueError):
        arc.controller_versions(plan(None))


def test_unrelated_or_unchanged_plans_do_not_require_cluster_reads():
    assert arc.controller_versions({}) == []
    assert arc.controller_versions(plan(actions=["no-op"])) == []


def test_initial_install_without_scale_sets_is_allowed():
    arc.verify(plan(actions=["create"]), [])
