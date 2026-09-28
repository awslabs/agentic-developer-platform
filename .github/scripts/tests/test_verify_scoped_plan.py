"""Tests for the scoped-apply plan guard (#5006).

Gate coverage, taken one-for-one from the issue's ``## Validation`` list and
its impact table. Each test states what it proves:

* Positive: the reviewed ConfigMap as a ``create``, and as an ``update``, pass.
* Collateral: the expected ConfigMap plus *any* other non-no-op change refuses
  (the #5003 protection -- an unapproved ECR replacement must not ride along).
* Replacement in BOTH action orders refuses. This is the #5002 gap: a gate that
  greps for ``will be destroyed`` never sees ``must be replaced``, so the
  assertion is on the exact action list rather than on a substring.
* Enablement refuses deletes; rollback has a separate exact-delete contract.
* Wrong address, wrong namespace, wrong name, and every ``data`` deviation
  (absent key, ``"false"``, extra key) refuse.
* Malformed input -- empty file, non-JSON, JSON without ``resource_changes``,
  and a non-zero ``terraform show`` exit -- refuses. Asserted explicitly
  because the dangerous failure mode is reading these as "zero changes,
  proceed".
* ``after_unknown`` over an asserted field refuses: an unknown value means the
  reviewed plan is not provably the applied plan.
* An unrecognised scope refuses rather than falling through to a full apply,
  and ``full`` is refused as a *guard subject* while remaining a valid input.
* Account/identity disagreement refuses.
* Log hygiene: guard output carries only addresses and action verbs, and no
  attribute value from the plan -- asserted with a canary secret planted in the
  plan's attributes.

no-op entries are present in most fixtures on purpose: the guard must filter by
action rather than by count of ``resource_changes``.
"""

import json
import os
import re
import subprocess

import yaml
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import verify_scoped_plan as vsp
from verify_scoped_plan import FULL_SCOPE, SCOPES, main

SCOPE = "network-policy-controller"
ADDRESS = "module.eks.kubernetes_config_map.amazon_vpc_cni[0]"
ACCOUNT = "879318057152"

# Planted in fixture attribute values. If it ever reaches stdout, the guard is
# echoing plan content and could leak a real secret the same way.
CANARY = "s3cr3t-canary-value"


# --------------------------------------------------------------------------
# fixture builders
# --------------------------------------------------------------------------
def configmap_change(actions=("create",), *, name="amazon-vpc-cni",
                     namespace="kube-system", data=None, address=ADDRESS,
                     after_unknown=None, after=True):
    if data is None:
        data = {"enable-network-policy-controller": "true"}
    entry = {
        "address": address,
        "type": "kubernetes_config_map",
        "change": {"actions": list(actions), "after_unknown": {}},
    }
    if after:
        entry["change"]["after"] = {
            "metadata": [{"name": name, "namespace": namespace}],
            "data": data,
        }
    if after_unknown is not None:
        entry["change"]["after_unknown"] = after_unknown
    return entry


def noop_change(address="module.eks.aws_eks_cluster.main"):
    return {
        "address": address,
        "change": {"actions": ["no-op"], "after": {"tags": {"canary": CANARY}}},
    }


def other_change(address="module.ecr.aws_ecr_repository.gateway",
                 actions=("delete", "create")):
    return {
        "address": address,
        "change": {
            "actions": list(actions),
            "after": {"encryption_configuration": [{"kms_key": CANARY}]},
        },
    }


def write_plan(tmp_path, *changes, raw=None, key="resource_changes"):
    path = tmp_path / "plan.json"
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
    else:
        path.write_text(json.dumps({key: list(changes)}), encoding="utf-8")
    return str(path)


def invoke(plan_path, *, scope=SCOPE, show_exit=0, resolved=ACCOUNT,
           identity=ACCOUNT, requested="", mode="verify-plan"):
    argv = [
        "--mode", mode,
        "--scope", scope,
        "--plan-json", str(plan_path),
        "--show-exit-code", str(show_exit),
        "--resolved-account", resolved,
        "--identity-account", identity,
    ]
    if requested:
        argv += ["--requested-account", requested]
    return main(argv)


# --------------------------------------------------------------------------
# positive cases
# --------------------------------------------------------------------------
def test_create_of_reviewed_configmap_passes(tmp_path, capsys):
    """The intended enablement -- one create of the exact ConfigMap -- passes."""
    plan = write_plan(tmp_path, noop_change(), configmap_change(["create"]))
    assert invoke(plan) == 0
    assert "PASS" in capsys.readouterr().out


def test_update_of_reviewed_configmap_passes(tmp_path):
    """An in-place update to the same reviewed object also passes."""
    plan = write_plan(tmp_path, configmap_change(["update"]))
    assert invoke(plan) == 0


def test_omitted_requested_account_is_allowed(tmp_path):
    """No --requested-account is legitimate: config/runtime resolves it."""
    plan = write_plan(tmp_path, configmap_change())
    assert invoke(plan, requested="") == 0


def test_requested_account_matching_resolved_passes(tmp_path):
    plan = write_plan(tmp_path, configmap_change())
    assert invoke(plan, requested=ACCOUNT) == 0


# --------------------------------------------------------------------------
# collateral -- the #5003 protection
# --------------------------------------------------------------------------
def test_configmap_plus_any_other_change_refuses(tmp_path):
    """Collateral of any kind refuses, even with the right ConfigMap present."""
    plan = write_plan(tmp_path, configmap_change(), other_change())
    assert invoke(plan) == 1


def test_collateral_tags_only_update_refuses(tmp_path):
    """Even a benign tags-only update is collateral and refuses."""
    plan = write_plan(
        tmp_path,
        configmap_change(),
        other_change(address='module.eks.aws_eks_access_entry.admins["x"]',
                     actions=("update",)),
    )
    assert invoke(plan) == 1


def test_zero_changes_refuses(tmp_path):
    """An empty plan is not a pass: nothing would be applied, so the operator
    must not be told the enablement landed."""
    plan = write_plan(tmp_path, noop_change())
    assert invoke(plan) == 1


# --------------------------------------------------------------------------
# replacement / delete -- the #5002 gap
# --------------------------------------------------------------------------
@pytest.mark.parametrize("actions", [
    ["delete", "create"],
    ["create", "delete"],
    ["delete"],
])
def test_any_delete_refuses(tmp_path, actions):
    """Replacement in either order, and pure delete, all refuse."""
    plan = write_plan(tmp_path, configmap_change(actions))
    assert invoke(plan) == 1


def test_read_action_refuses(tmp_path):
    """Only create/update are permitted; anything else refuses."""
    plan = write_plan(tmp_path, configmap_change(["read"]))
    assert invoke(plan) == 1


# --------------------------------------------------------------------------
# wrong address / wrong object / wrong value
# --------------------------------------------------------------------------
@pytest.mark.parametrize("address", [
    "module.eks.kubernetes_config_map.amazon_vpc_cni[1]",
    "module.eks.kubernetes_config_map.something_else[0]",
    "module.eks.kubernetes_config_map.amazon_vpc_cni",
    "kubernetes_config_map.amazon_vpc_cni[0]",
])
def test_wrong_address_refuses(tmp_path, address):
    """Right resource type, different name/index/module path -> refuses."""
    plan = write_plan(tmp_path, configmap_change(address=address))
    assert invoke(plan) == 1


@pytest.mark.parametrize("kwargs", [
    {"namespace": "default"},
    {"namespace": ""},
    {"name": "aws-node"},
    {"name": "amazon-vpc-cni-2"},
])
def test_wrong_object_identity_refuses(tmp_path, kwargs):
    """Correct address but the wrong namespace or name -> refuses."""
    plan = write_plan(tmp_path, configmap_change(**kwargs))
    assert invoke(plan) == 1


@pytest.mark.parametrize("data", [
    {},
    {"enable-network-policy-controller": "false"},
    {"other-key": "true"},
    {"enable-network-policy-controller": "true", "extra": "x"},
    {"enable-network-policy-controller": True},
])
def test_wrong_data_refuses(tmp_path, data):
    """Absent key, false value, an extra key, or a non-string true -> refuses.
    An extra key is a different ConfigMap than the one reviewed."""
    plan = write_plan(tmp_path, configmap_change(data=data))
    assert invoke(plan) == 1


def test_missing_after_object_refuses(tmp_path):
    plan = write_plan(tmp_path, configmap_change(after=False))
    assert invoke(plan) == 1


def test_missing_metadata_block_refuses(tmp_path):
    entry = configmap_change()
    entry["change"]["after"]["metadata"] = []
    plan = write_plan(tmp_path, entry)
    assert invoke(plan) == 1


# --------------------------------------------------------------------------
# malformed input -- must never read as "zero changes, proceed"
# --------------------------------------------------------------------------
def test_empty_file_refuses(tmp_path):
    plan = write_plan(tmp_path, raw="")
    assert invoke(plan) == 1


def test_non_json_refuses(tmp_path):
    plan = write_plan(tmp_path, raw="Error: could not read plan file\n")
    assert invoke(plan) == 1


def test_json_without_resource_changes_refuses(tmp_path):
    """Terraform emits the key even for an empty plan; absence means this is
    not a plan document, not that there is nothing to do."""
    plan = write_plan(tmp_path, raw=json.dumps({"format_version": "1.2"}))
    assert invoke(plan) == 1


def test_resource_changes_wrong_type_refuses(tmp_path):
    plan = write_plan(tmp_path, raw=json.dumps({"resource_changes": {"a": 1}}))
    assert invoke(plan) == 1


def test_json_not_an_object_refuses(tmp_path):
    plan = write_plan(tmp_path, raw=json.dumps([1, 2, 3]))
    assert invoke(plan) == 1


def test_terraform_show_nonzero_exit_refuses(tmp_path):
    """A failed show cannot be approved even if a stale file parses fine."""
    plan = write_plan(tmp_path, configmap_change())
    assert invoke(plan, show_exit=1) == 1


def test_show_exit_code_defaults_to_refusal(tmp_path):
    """Omitting --show-exit-code refuses: the default fails closed."""
    plan = write_plan(tmp_path, configmap_change())
    assert main([
        "--scope", SCOPE, "--plan-json", str(plan),
        "--resolved-account", ACCOUNT, "--identity-account", ACCOUNT,
    ]) == 1


def test_missing_plan_file_refuses(tmp_path):
    assert invoke(str(tmp_path / "does-not-exist.json")) == 1


def test_entry_without_change_refuses(tmp_path):
    plan = write_plan(tmp_path, {"address": ADDRESS})
    assert invoke(plan) == 1


def test_entry_without_actions_refuses(tmp_path):
    plan = write_plan(tmp_path, {"address": ADDRESS, "change": {"after": {}}})
    assert invoke(plan) == 1


# --------------------------------------------------------------------------
# unknown values
# --------------------------------------------------------------------------
@pytest.mark.parametrize("after_unknown", [
    {"data": True},
    {"metadata": True},
    {"data": {"enable-network-policy-controller": True}},
    {"metadata": [{"namespace": True}]},
    True,
])
def test_after_unknown_over_asserted_field_refuses(tmp_path, after_unknown):
    """If the plan does not know what it will write, the reviewed plan is not
    provably the applied plan."""
    plan = write_plan(tmp_path, configmap_change(after_unknown=after_unknown))
    assert invoke(plan) == 1


def test_after_unknown_on_unasserted_field_passes(tmp_path):
    """Unknowns we do not assert over (e.g. a generated id) are not a refusal
    -- otherwise the guard would refuse every legitimate create."""
    plan = write_plan(tmp_path, configmap_change(after_unknown={"id": True}))
    assert invoke(plan) == 0


# --------------------------------------------------------------------------
# scope handling
# --------------------------------------------------------------------------
@pytest.mark.parametrize("scope", ["", "unknown-scope", "network-policy", "FULL",
                                   "network-policy-controller extra"])
def test_unrecognised_scope_refuses(tmp_path, scope):
    """An unrecognised scope must refuse, never fall through to a full apply."""
    plan = write_plan(tmp_path, configmap_change())
    assert invoke(plan, scope=scope) == 1


def test_full_scope_is_not_a_guard_subject(tmp_path):
    """The guard must not be usable to bless a full apply."""
    plan = write_plan(tmp_path, configmap_change())
    assert invoke(plan, scope=FULL_SCOPE) == 1


@pytest.mark.parametrize("scope", [FULL_SCOPE, SCOPE])
def test_validate_scope_accepts_known_scopes(scope):
    """Both workflow choice options are recognised names."""
    assert main(["--mode", "validate-scope", "--scope", scope]) == 0


@pytest.mark.parametrize("scope", ["", "unknown-scope", "Full"])
def test_validate_scope_rejects_unknown(scope):
    """A choice option added without a SCOPES entry fails closed."""
    assert main(["--mode", "validate-scope", "--scope", scope]) == 1


def test_enable_scope_permits_no_delete():
    """Structural: no scope may ever permit a delete without a deliberate,
    separately-reviewed change to this table."""
    for spec in [SCOPES[SCOPE]]:
        for actions in spec["allowed_actions"]:
            assert "delete" not in actions


# --------------------------------------------------------------------------
# account / identity
# --------------------------------------------------------------------------
def test_identity_account_mismatch_refuses(tmp_path):
    """Guarding plan content is worthless if it is applied to another account."""
    plan = write_plan(tmp_path, configmap_change())
    assert invoke(plan, resolved=ACCOUNT, identity="111122223333") == 1


def test_requested_account_mismatch_refuses(tmp_path):
    plan = write_plan(tmp_path, configmap_change())
    assert invoke(plan, requested="111122223333") == 1


@pytest.mark.parametrize("resolved,identity", [("", ACCOUNT), (ACCOUNT, "")])
def test_empty_account_refuses(tmp_path, resolved, identity):
    """An unresolvable account is an unknown, and an unknown is not a pass."""
    plan = write_plan(tmp_path, configmap_change())
    assert invoke(plan, resolved=resolved, identity=identity) == 1


# --------------------------------------------------------------------------
# log hygiene
# --------------------------------------------------------------------------
def test_output_contains_no_plan_attribute_values(tmp_path, capsys):
    """Plan JSON can carry secrets, so the guard emits only addresses and
    action verbs. The canary lives in attribute values of both a no-op and a
    collateral change; neither may reach stdout."""
    plan = write_plan(tmp_path, noop_change(), configmap_change(), other_change())
    assert invoke(plan) == 1
    out = capsys.readouterr().out
    assert CANARY not in out
    assert "encryption_configuration" not in out
    # The allowlisted fields ARE present, so the operator can still review.
    assert ADDRESS in out
    assert "module.ecr.aws_ecr_repository.gateway" in out


def test_output_is_not_raw_json(tmp_path, capsys):
    plan = write_plan(tmp_path, configmap_change())
    invoke(plan)
    out = capsys.readouterr().out
    assert "resource_changes" not in out
    assert '"after"' not in out


def test_refusal_reason_does_not_echo_found_value(tmp_path, capsys):
    """A wrong-value refusal names the field, not the value found -- the value
    is plan content."""
    plan = write_plan(tmp_path, configmap_change(data={"leaky": CANARY}))
    assert invoke(plan) == 1
    assert CANARY not in capsys.readouterr().out


# --------------------------------------------------------------------------
# the workflow contract this guard is wired into
# --------------------------------------------------------------------------
WORKFLOW = Path(__file__).resolve().parents[2] / "workflows" / "platform-infra-apply.yml"


def _workflow_text():
    return WORKFLOW.read_text(encoding="utf-8")


def test_workflow_declares_scope_as_a_choice():
    """type: choice is what makes an operator-supplied target address
    impossible -- the value selects a branch, it is never interpolated into a
    terraform command."""
    text = _workflow_text()
    assert "scope:" in text
    assert "type: choice" in text
    assert "default: 'full'" in text


def test_workflow_has_no_free_text_target_input():
    """A free-text target input would make this a general-purpose
    'apply anything unaudited' primitive."""
    text = _workflow_text()
    assert "inputs.target" not in text
    assert "-target=${{" not in text


def test_workflow_target_address_is_a_hardcoded_literal():
    """The scoped plan's target must be a literal in the workflow, and no
    -target may be fed from any operator-controlled expression."""
    text = _workflow_text()
    assert f"'{ADDRESS}'" in text, "the scope's address is not present as a literal"
    for line in text.splitlines():
        if "-target" in line:
            assert "${{" not in line, f"-target takes a GitHub expression: {line.strip()}"
            assert "inputs." not in line, f"-target takes an input: {line.strip()}"


def test_workflow_applies_the_saved_plan_not_a_replan():
    """The reviewed plan must be the applied plan (the #5004 failure mode)."""
    text = _workflow_text()
    assert "terraform apply tfplan" in text


def test_workflow_scoped_path_keeps_the_destroy_gate():
    """The new guard is additional, not a replacement, and it must not pass
    confirm_destructive_apply or add a destructive-apply-approved label."""
    text = _workflow_text()
    assert "Destroy-safety gate" in text
    assert text.count("confirm_destructive_apply") >= 1
    # The guard step itself must not grant the destructive authorisation.
    guard_region = text.split("Verify scoped plan")[-1].split("- name:")[0]
    assert "CONFIRM_DESTRUCTIVE_APPLY: ${{ inputs.confirm_destructive_apply }}" in guard_region
    assert "destructive-apply-approved" not in guard_region


def test_workflow_invokes_the_checked_in_guard():
    """Inline shell cannot be unit-tested; #5002 exists because a
    security-critical predicate was duplicated inline."""
    assert "verify_scoped_plan.py" in _workflow_text()


def test_script_tests_workflow_registers_this_suite():
    """#4483: a test job that pins its file list but globs its trigger can pass
    by never running. Both the pytest list and the path filters must name the
    new files."""
    reg = (WORKFLOW.parent / "script-tests.yml").read_text(encoding="utf-8")
    assert "tests/test_verify_scoped_plan.py" in reg
    assert ".github/scripts/verify_scoped_plan.py" in reg

    # Every non-script file this suite asserts against must appear in BOTH
    # triggers, or an edit to it skips the gate that guards it.
    triggers = yaml.safe_load(reg)[True]
    for trigger in ("push", "pull_request"):
        paths = triggers[trigger]["paths"]
        for subject in (".github/workflows/platform-infra-apply.yml",
                        "platform/infra/modules/eks/main.tf",
                        "modules/agent-factory/infra/main.tf"):
            assert subject in paths, f"{subject} missing from {trigger}"


ROLLBACK = 'network-policy-controller-rollback'
REPO = WORKFLOW.parents[2]


def rollback_change():
    entry = configmap_change(['delete'])
    entry['change']['before'] = entry['change']['after']
    entry['change']['after'] = None
    return entry


def invoke_rollback(path, confirmation='yes'):
    return main(['--scope', ROLLBACK, '--plan-json', str(path),
                 '--show-exit-code', '0', '--resolved-account', ACCOUNT,
                 '--identity-account', ACCOUNT,
                 '--confirm-destructive-apply', confirmation])


def test_real_provider_create_metadata_passes(tmp_path):
    # Shape extracted from the real saved scoped plan on #5006.
    entry = configmap_change(after_unknown={
        'data': {}, 'id': True,
        'metadata': [{'generation': True, 'resource_version': True, 'uid': True}],
    })
    entry['change']['after']['metadata'][0].update(
        annotations=None, generate_name=None, labels=None)
    assert invoke(write_plan(tmp_path, entry)) == 0


@pytest.mark.parametrize('unknown', [
    None, False, [], '', {'metadata': []}, {'metadata': {}},
    {'metadata': [True]}, {'metadata': [{'name': True}]},
    {'metadata': [{'namespace': True}]}, {'metadata': [{'name': None}]},
    {'data': []}, {'data': None}, {'data': {'extra': True}},
])
def test_missing_malformed_or_asserted_unknown_refuses(tmp_path, unknown):
    entry = configmap_change()
    entry['change']['after_unknown'] = unknown
    assert invoke(write_plan(tmp_path, entry)) == 1


def test_missing_unknownness_refuses(tmp_path):
    entry = configmap_change()
    del entry['change']['after_unknown']
    assert invoke(write_plan(tmp_path, entry)) == 1


def test_confirmed_rollback_of_exact_object_passes(tmp_path):
    assert invoke_rollback(write_plan(tmp_path, noop_change(), rollback_change())) == 0


@pytest.mark.parametrize('confirmation', ['', 'no', 'true', 'YES'])
def test_rollback_requires_existing_explicit_confirmation(tmp_path, confirmation):
    assert invoke_rollback(write_plan(tmp_path, rollback_change()), confirmation) == 1


@pytest.mark.parametrize('actions', [['create'], ['update'], ['delete', 'create'], ['create', 'delete']])
def test_rollback_rejects_non_delete_actions(tmp_path, actions):
    entry = rollback_change()
    entry['change']['actions'] = actions
    assert invoke_rollback(write_plan(tmp_path, entry)) == 1


@pytest.mark.parametrize('mutation', ['address', 'name', 'namespace', 'data', 'extra', 'after', 'before'])
def test_rollback_rejects_wrong_object(tmp_path, mutation):
    entry = rollback_change()
    if mutation == 'address':
        entry['address'] = ADDRESS.replace('[0]', '[1]')
    elif mutation in ('name', 'namespace'):
        entry['change']['before']['metadata'][0][mutation] = 'other'
    elif mutation == 'data':
        entry['change']['before']['data'] = {'enable-network-policy-controller': 'false'}
    elif mutation == 'extra':
        entry['change']['before']['data']['extra'] = CANARY
    elif mutation == 'after':
        entry['change']['after'] = {}
    else:
        del entry['change']['before']
    assert invoke_rollback(write_plan(tmp_path, entry)) == 1


def test_rollback_rejects_collateral(tmp_path):
    assert invoke_rollback(write_plan(tmp_path, rollback_change(), other_change())) == 1


def workflow_step(name):
    return next(s for s in yaml.safe_load(_workflow_text())['jobs']['apply']['steps']
                if s.get('name') == name)


def execute_step(tmp_path, name, **overrides):
    env = dict(os.environ, GITHUB_WORKSPACE=str(REPO), APPLY_SCOPE=SCOPE,
               GITHUB_OUTPUT=str(tmp_path / 'output'),
               GITHUB_STEP_SUMMARY=str(tmp_path / 'summary'),
               GITHUB_ENV=str(tmp_path / 'env'),
               SCOPED_VAR_FILE='environments/dev/platform.tfvars',
               ACCOUNT_ID=ACCOUNT, REQUESTED_ACCOUNT=ACCOUNT,
               CONFIRM_DESTRUCTIVE_APPLY='no',
               ENVIRONMENT='dev', AWS_REGION='us-east-1', CUSTOMER_ACCOUNT_ID='')
    env.update(overrides)
    return subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', workflow_step(name)['run']],
                          cwd=REPO, env=env, capture_output=True, text=True)


def fake_cli(tmp_path, name, body):
    path = tmp_path / name
    path.write_text('#!/bin/bash\n' + body)
    path.chmod(0o755)
    return str(tmp_path) + os.pathsep + os.environ['PATH']


@pytest.mark.parametrize('payload', [
    '$(touch MARKER; printf full)', '`touch MARKER`',
    '\"; touch MARKER; #', 'full\ntouch MARKER', ' full ',
])
@pytest.mark.parametrize('step', ['Validate apply scope', 'Terraform Plan (scoped)'])
def test_workflow_rejects_scope_as_data_without_side_effect(tmp_path, payload, step):
    marker = tmp_path / 'executed'
    payload = payload.replace('MARKER', str(marker))
    result = execute_step(tmp_path, step, APPLY_SCOPE=payload)
    assert result.returncode != 0
    assert not marker.exists()


@pytest.mark.parametrize('step', ['Validate apply scope', 'Terraform Plan (scoped)', 'Verify scoped plan',
                                 'Verify scoped target and preserve EKS public access CIDRs'])
def test_new_steps_do_not_interpolate_expressions_into_shell(step):
    assert '${{' not in workflow_step(step)['run']


@pytest.mark.parametrize('code,accepted', [(0, True), (2, True), (1, False), (3, False), (137, False)])
def test_scoped_plan_exit_status_is_fail_closed(tmp_path, code, accepted):
    path = fake_cli(tmp_path, 'terraform', 'printf "%s\\n" "$@" > "$GITHUB_OUTPUT.args"\nexit ' + str(code))
    result = execute_step(tmp_path, 'Terraform Plan (scoped)', PATH=path)
    assert (result.returncode == 0) == accepted
    args = (tmp_path / 'output.args').read_text().splitlines()
    assert '-target=' + ADDRESS in args
    assert '-out=tfplan' in args


def test_rollback_plan_has_fixed_false_override(tmp_path):
    path = fake_cli(tmp_path, 'terraform', 'printf "%s\\n" "$@" > "$GITHUB_OUTPUT.args"\nexit 2')
    result = execute_step(tmp_path, 'Terraform Plan (scoped)', PATH=path, APPLY_SCOPE=ROLLBACK)
    assert result.returncode == 0
    args = (tmp_path / 'output.args').read_text().splitlines()
    assert args.index('-var=enable_network_policy_controller=false') > args.index('-var-file=../../environments/dev/platform.tfvars')
    assert '-target=' + ADDRESS in args


@pytest.mark.parametrize('show_code,identity_code,valid_plan', [(0, 0, True), (0, 0, False), (1, 0, True), (0, 1, True)])
def test_guard_step_cleans_plan_and_propagates_failures(tmp_path, show_code, identity_code, valid_plan):
    entry = configmap_change() if valid_plan else other_change()
    plan = write_plan(tmp_path, entry)
    path = fake_cli(tmp_path, 'terraform', 'cat "$TEST_PLAN"\nexit ' + str(show_code))
    fake_cli(tmp_path, 'aws', 'echo ' + ACCOUNT + '\nexit ' + str(identity_code))
    result = execute_step(tmp_path, 'Verify scoped plan', PATH=path, TEST_PLAN=plan)
    assert (result.returncode == 0) == (show_code == 0 and identity_code == 0 and valid_plan)
    assert not Path('/tmp/scoped-plan.json').exists()
    assert not Path('/tmp/scoped-plan.err').exists()
    if identity_code == 0:
        summary = (tmp_path / 'summary').read_text()
        assert ('PASS:' if valid_plan and show_code == 0 else 'FAIL:') in summary
        assert CANARY not in summary


def test_requested_account_is_data_not_shell(tmp_path):
    marker = tmp_path / 'executed'
    path = fake_cli(tmp_path, 'aws', 'echo ' + ACCOUNT)
    result = execute_step(tmp_path, 'Verify scoped target and preserve EKS public access CIDRs',
                          PATH=path, REQUESTED_ACCOUNT='$(touch ' + str(marker) + ')')
    assert result.returncode != 0
    assert not marker.exists()


def test_scoped_path_preserves_live_cidrs(tmp_path):
    path = fake_cli(tmp_path, 'aws', '''case "$*" in
      *get-caller-identity*) echo 879318057152 ;;
      *cluster.arn*) echo arn:aws:eks:us-east-1:879318057152:cluster/adp-dev-eks-cluster ;;
      *publicAccessCidrs*) echo '["198.51.100.0/24", "203.0.113.4/32"]' ;;
      *) exit 1 ;;
    esac''')
    result = execute_step(tmp_path, 'Verify scoped target and preserve EKS public access CIDRs', PATH=path)
    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / 'env').read_text().split('=', 1)[1]) == ['198.51.100.0/24', '203.0.113.4/32']


def test_plan_only_skips_apply_and_scoped_setup_skips_bedrock_mutation():
    assert workflow_step('Terraform Apply')['if'] == '${{ !inputs.plan_only }}'
    assert workflow_step('Enable Bedrock model access')['if'] == "inputs.scope == '' || inputs.scope == 'full'"


def test_access_permissions_and_propagation_remain_managed():
    source = (REPO / 'platform/infra/modules/eks/main.tf').read_text()
    entry = source.split('resource "aws_eks_access_entry" "admins" {')[1].split('\nresource ')[0]
    assert 'ignore_changes' not in entry
    assert 'principal_arn = each.key' in entry
    assert 'type          = "STANDARD"' in entry
    association = source.split('resource "aws_eks_access_policy_association" "admins" {')[1].split('\nresource ')[0]
    assert 'principal_arn = each.value.principal_arn' in association
    assert 'AmazonEKSClusterAdminPolicy' in association
    assert 'type = "cluster"' in association
    assert 'depends_on      = [aws_eks_access_policy_association.admins]' in source
    cm = source.split('resource "kubernetes_config_map" "amazon_vpc_cni" {')[1]
    assert 'time_sleep.wait_for_access_entry' in cm


def _hcl_tag_map(text):
    """Extract `Key = "value"` pairs, tolerating `${...}` inside the value.

    Splitting an HCL block on ``}`` truncates at an interpolation's closing
    brace, so the tag values are matched directly instead.
    """
    return dict(re.findall(r'^\s*(\w+)\s*=\s*"((?:[^"\\]|\\.)*)"\s*$', text, re.M))


def test_platform_runner_tags_match_agent_factorys_canonical_declaration():
    """The two modules must agree on the shared runner entry's tags (#5006).

    Both ``platform/infra/modules/eks`` and ``modules/agent-factory/infra``
    declare an access entry for the same CI runner principal, so whichever
    applies last wins. When they disagree, every platform plan carries a
    permanent tags-only diff -- which appears as collateral in the -target'ed
    plan and the guard (correctly) refuses, leaving the scoped route unusable.

    This is asserted here, in the suite ``script-tests.yml`` actually runs,
    rather than only in ``runner_access_tags.tftest.hcl``: no workflow in this
    repo executes ``terraform test``, so a tftest-only assertion is not
    merge-blocking. Derived from BOTH sources so drift on either side fails.
    """
    platform = (REPO / 'platform/infra/modules/eks/main.tf').read_text()
    factory = (REPO / 'modules/agent-factory/infra/main.tf').read_text()

    # agent-factory owns the entry's explicit tags plus the Module/Owner it
    # inherits from that module's provider default_tags.
    factory_entry = factory.split('resource "aws_eks_access_entry" "runner" {')[1].split('\nresource ')[0]
    factory_defaults = _hcl_tag_map(factory.split('default_tags {')[1].split('\n  }')[0])
    name_prefix = _hcl_tag_map(factory.split('\nlocals {')[1].split('\n}')[0])['name_prefix']
    expected = {
        'Name': _hcl_tag_map(factory_entry.split('tags = {')[1])['Name']
                .replace('${local.name_prefix}', name_prefix),
        'Module': factory_defaults['Module'],
        'Owner': factory_defaults['Owner'],
    }
    assert expected == {'Name': 'adp-${var.environment}-agent-runner-access',
                        'Module': 'agent-factory', 'Owner': 'agent-team'}

    # Platform must declare exactly those, and only for the runner principal --
    # a blanket override would blind platform to drift on entries it does own.
    entry = platform.split('resource "aws_eks_access_entry" "admins" {')[1].split('\nresource ')[0]
    conditional = entry.split('tags = ')[1]
    assert _hcl_tag_map(conditional.split('} :')[0]) == expected
    assert conditional.split('} :')[1].strip().startswith('{}')

    # The selector must be equality against the runner principal alone. An
    # always-true condition (or any other comparison) would apply the override
    # to every admin entry, which is the blanket suppression #5006 forbids.
    condition = conditional.split('?')[0].strip()
    assert re.fullmatch(
        r'each\.key == "arn:aws:iam::\$\{data\.aws_caller_identity\.current\.account_id\}'
        r':role/\$\{var\.name_prefix\}-agent-runner-role"', condition), condition
