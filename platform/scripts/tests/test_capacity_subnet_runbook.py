#!/usr/bin/env python3
"""The runbook's rollout shell must stop on a refusal, never plan past it (#5830).

The bug this suite exists to prevent is one line of shell, and it is invisible to
review: `export VAR="$(resolver ...)"` reports *export's* exit status, not the
resolver's. So when the resolver deliberately refuses -- which is how it protects
the live subnet set from being planned away -- the step looks successful, VAR is
empty, and the very next `terraform plan` removes the additions. The refusal
machinery stays intact; the documented way of using it discards the answer.

Grepping the runbook for a command shape would not catch that: the broken version
contains the same words. So these tests EXTRACT the fenced blocks from
docs/runbooks/eks-pod-ip-exhaustion.md and EXECUTE them with a refusing resolver
and a `terraform` that records every invocation. The contract is behavioural: the
block exits non-zero and `terraform` was never called at all.

No AWS call and no real terraform run: `python3`, `terraform` and `aws` are all
shimmed on PATH for the duration.
"""
import os
from pathlib import Path
import re
import stat
import subprocess

import pytest

REPO = Path(__file__).resolve().parents[3]
RUNBOOK = REPO / "docs/runbooks/eks-pod-ip-exhaustion.md"

# Marker comments inside the fenced blocks. They are part of the documented text
# so that an edit cannot quietly move the tested commands out of the test's reach:
# a missing marker fails extraction rather than silently covering nothing.
BLOCK = re.compile(r"```bash\n# runbook-block: (?P<name>[a-z-]+)\n(?P<body>.*?)```", re.DOTALL)


def blocks():
    found = {m.group("name"): m.group("body") for m in BLOCK.finditer(RUNBOOK.read_text())}
    return found


def write(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def harness(tmp_path, resolver_exit=0, resolver_out='{"us-east-1a": "subnet-0e57a000000000001"}'):
    """PATH shims for python3/terraform/aws, plus a log of terraform invocations.

    The python3 shim dispatches on the script being run, so the resolver can refuse
    while upgrade-state.py still produces the tfvars file the plan expects -- which
    is what lets the test distinguish "stopped at the refusal" from "stopped because
    something else was missing".
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "terraform-invocations.log"
    write(bin_dir / "terraform", f'#!/bin/sh\nprintf "%s\\n" "$*" >> {log}\nexit 0\n')
    write(bin_dir / "aws", '#!/bin/sh\necho "{}"\n')
    write(bin_dir / "python3", f"""#!/bin/sh
case "$*" in
  *resolve-capacity-subnets.py*)
    if [ {resolver_exit} -ne 0 ]; then
      echo "::error::Configured additional capacity subnets omit subnet(s) the live cluster is using" >&2
      exit {resolver_exit}
    fi
    printf '%s\\n' '{resolver_out}'
    ;;
  *upgrade-state.py*)
    # Mimic prepare(): a 0600 tfvars file in the private run directory.
    dir=""
    while [ "$#" -gt 0 ]; do
      case "$1" in --directory) dir="$2" ;; esac
      shift
    done
    printf '%s\\n' '{{"environment":"dev"}}' > "$dir/platform.tfvars.json"
    chmod 600 "$dir/platform.tfvars.json"
    ;;
  *) echo "unexpected python3 invocation: $*" >&2; exit 97 ;;
esac
""")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "TMPDIR": str(tmp_path),
           "ENV": "dev", "ACCOUNT_ID": "000000000000", "STATE_BUCKET": "adp-terraform-state-000000000000",
           "REDUCED_MAP": "{}"}
    env.pop("ADDITIONAL_PRIVATE_SUBNETS_BY_AZ", None)
    return env, log


def run(script, env):
    return subprocess.run(["bash", "-c", script], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=120)


# ---------------------------------------------------------------------------
# Extraction — a test that cannot find its subject must fail, not pass silently
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["resolve-inputs", "scoped-plan", "rollback"])
def test_the_runbook_still_carries_the_block_this_suite_executes(name):
    assert name in blocks(), (
        f"docs/runbooks/eks-pod-ip-exhaustion.md no longer has a '# runbook-block: {name}' "
        "block; the rollout shell would be undocumented or untested")


# ---------------------------------------------------------------------------
# The failure-masking regression itself
# ---------------------------------------------------------------------------

def test_a_refusing_resolver_stops_the_rollout_before_any_terraform_runs(tmp_path):
    # THE regression. With `export VAR="$(resolver)"` this passed the refusal and
    # planned the additions away; the assertion that matters is the empty log.
    env, log = harness(tmp_path, resolver_exit=1)
    found = blocks()
    result = run(found["resolve-inputs"] + "\n" + found["scoped-plan"], env)
    assert result.returncode != 0, (
        "the documented rollout shell swallowed the resolver's refusal:\n" + result.stdout)
    assert not log.exists(), (
        "terraform ran despite the resolver refusing -- the plan would remove the live "
        f"capacity subnets:\n{log.read_text()}")


def test_the_refusal_reaches_the_operator(tmp_path):
    env, _ = harness(tmp_path, resolver_exit=1)
    result = run(blocks()["resolve-inputs"], env)
    assert "omit subnet(s) the live cluster is using" in result.stderr


def test_a_refusing_resolver_also_stops_the_rollback_path(tmp_path):
    # Rollback authorises removal, so a refusal there is rarer -- but the same
    # masked-export shape would plan a map the operator never chose.
    env, log = harness(tmp_path, resolver_exit=1)
    result = run(blocks()["rollback"], env)
    assert result.returncode != 0 and not log.exists()


# ---------------------------------------------------------------------------
# ...and the same blocks must still work when the resolver succeeds. A "fix" that
# refuses everything would pass the tests above.
# ---------------------------------------------------------------------------

def test_a_successful_resolution_plans_with_the_resolved_value(tmp_path):
    env, log = harness(tmp_path)
    found = blocks()
    result = run(found["resolve-inputs"] + "\n" + found["scoped-plan"], env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "subnet-0e57a000000000001" in result.stdout, "the resolved map was not echoed for review"
    invocations = log.read_text().splitlines()
    plan = next(line for line in invocations if line.startswith("plan"))
    # Targeted at the cluster, and saved to a file rather than applied directly.
    assert "-target=module.eks.aws_eks_cluster.main" in plan
    assert "-out=" in plan and "subnets.tfplan" in plan
    # The reviewed plan is rendered from the saved file, not re-planned.
    assert any(line.startswith("show -json") for line in invocations)


def test_the_plan_passes_the_prepared_inputs_after_the_committed_tfvars(tmp_path):
    # Precedence is last-wins, so the discovered live values must come AFTER the
    # repository overlay -- otherwise every account-specific input the operator did
    # not remember to export is its own silent narrowing.
    env, log = harness(tmp_path)
    found = blocks()
    assert run(found["resolve-inputs"] + "\n" + found["scoped-plan"], env).returncode == 0
    plan = next(line for line in log.read_text().splitlines() if line.startswith("plan"))
    committed = plan.index("environments/dev/platform.tfvars")
    prepared = plan.index("platform.tfvars.json")
    assert committed < prepared, f"prepared inputs must follow the committed var-file: {plan}"


def test_the_rollback_plan_uses_the_resolved_reduced_map(tmp_path):
    # The earlier version printed the reduced map and then never used it, so
    # following it literally planned something other than what was reviewed.
    env, log = harness(tmp_path, resolver_out="{}")
    result = run(blocks()["rollback"], env)
    assert result.returncode == 0, result.stdout + result.stderr
    plan = next(line for line in log.read_text().splitlines() if line.startswith("plan"))
    rollback_file = re.search(r'-var-file=(\S*rollback\.tfvars\.json)', plan)
    assert rollback_file, f"the resolved reduced map never reached the plan: {plan}"
    # Last var-file wins: the reduced map must override the discovered live set,
    # which is precisely the set being narrowed.
    assert plan.index("platform.tfvars.json") < plan.index("rollback.tfvars.json")
    written = Path(rollback_file.group(1)).read_text()
    assert '"additional_private_subnet_ids_by_az": {}' in written.replace('":{}', '": {}')


# ---------------------------------------------------------------------------
# Artefact handling — a saved plan carries real infrastructure values
# ---------------------------------------------------------------------------

def test_plans_are_written_to_a_private_run_directory_not_a_predictable_tmp_path(tmp_path):
    env, log = harness(tmp_path)
    found = blocks()
    assert run(found["resolve-inputs"] + "\n" + found["scoped-plan"], env).returncode == 0
    plan = next(line for line in log.read_text().splitlines() if line.startswith("plan"))
    out = re.search(r"-out=(\S+)", plan).group(1)
    directory = Path(out).parent
    assert directory != Path("/tmp"), "a saved plan must not sit at a predictable world-readable path"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700, oct(directory.stat().st_mode)


def test_the_documented_blocks_never_export_straight_from_a_command_substitution():
    # Belt-and-braces beside the executed tests above: the shape itself is the bug,
    # and naming it keeps a future edit from reintroducing it in a block that this
    # suite does not execute. Comment lines are excluded deliberately -- the blocks
    # quote the broken shape in prose to explain why it is wrong, and a check that
    # banned mentioning it would push that explanation out of the runbook.
    for name, body in blocks().items():
        commands = "\n".join(l for l in body.splitlines() if not l.lstrip().startswith("#"))
        assert not re.search(r'export\s+\w+="\$\(', commands), (
            f"runbook block '{name}' exports directly from a command substitution, which "
            "reports export's exit status and would hide a refusal")
