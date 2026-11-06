#!/usr/bin/env bash
set -euo pipefail
CLI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python3 - "$CLI_DIR" <<'PY'
import base64
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

cli = pathlib.Path(sys.argv[1])
with tempfile.TemporaryDirectory() as directory:
    home = pathlib.Path(directory)
    archive = home / ".adp/skills"
    archive.mkdir(parents=True)
    (archive / "MANIFEST.json").write_text(json.dumps({
        "schema_version": "1", "version": "1.0.0", "skills": [
            {"name": "adp-flow", "clients": ["claude-code", "codex"], "invocation": "adp flow"},
            {"name": "adp-plan", "clients": ["codex"], "invocation": "adp plan"},
        ],
    }))
    for name in ("adp-flow", "adp-plan"):
        skill = archive / name
        skill.mkdir()
        (skill / "SKILL.md").write_text(f"# {name}\n")
    config = home / ".bedrock-gateway"
    config.mkdir()
    (config / "config.json").write_text(json.dumps({"gateway_url": "https://example.invalid/api"}))
    claims = base64.urlsafe_b64encode(b'{"sub":"test-user","username":"tester"}').decode().rstrip("=")
    (config / "tokens.json").write_text(json.dumps({
        "refresh_token": "fake-refresh-token-long-enough", "access_token": f"e30.{claims}.signature",
        "expires_at": int(time.time()) + 3600,
    }))
    env = {key: value for key, value in os.environ.items() if not key.startswith(("ADP_", "BG_"))}
    env.update({"HOME": str(home), "ADP_HOME": str(home / ".adp")})
    claude = home / ".claude/skills/adp-flow"
    claude.mkdir(parents=True)
    (claude / "SKILL.md").write_text("personal work\n")

    def setup(client):
        result = subprocess.run(["bash", str(cli / "adp"), client, "setup"], env=env,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return result.stderr

    warning = setup("claude")
    assert str(claude) in warning and "Refusing unmarked" in warning, warning
    assert (claude / "SKILL.md").read_text() == "personal work\n"
    assert not (claude / ".adp-provenance.json").exists()
    assert not (home / ".claude/skills/adp-plan").exists()
    print("PASS: setup leaves a personal skill untouched and warns with its path")

    first = setup("codex")
    assert "Installed 2 ADP skills" in first, first
    for name in ("adp-flow", "adp-plan"):
        target = home / ".codex/skills" / name
        assert (target / "SKILL.md").read_text() == f"# {name}\n"
        marker = json.loads((target / ".adp-provenance.json").read_text())
        assert marker["source"] == "adp-cli" and marker["version"] == "1.0.0"
        assert len(marker["sha256"]) == 64
    second = setup("codex")
    assert "Installed 0 ADP skills" in second, second
    print("PASS: Codex setup filters skills, marks ownership and is idempotent")

    (claude / "SKILL.md").unlink()
    claude.rmdir()
    fresh = setup("claude")
    assert "Installed 1 ADP skills" in fresh, fresh
    assert (claude / "SKILL.md").read_text() == "# adp-flow\n"
    assert json.loads((claude / ".adp-provenance.json").read_text())["source"] == "adp-cli"
    assert "Installed 0 ADP skills" in setup("claude")
    print("PASS: Claude setup installs only its declared skills and is idempotent")

    def doctor():
        pinned = {**env, "ADP_TENANT_ID": "test-tenant", "ADP_TENANT_SUB": "test-user",
                  "ADP_TENANT_MODE": "legacy"}
        return subprocess.run(["bash", str(cli / "adp"), "doctor", "--checks", "skills"],
                              env=pinned, capture_output=True, text=True, timeout=30)

    healthy = doctor()
    assert healthy.returncode == 0, (healthy.stdout, healthy.stderr)
    assert "skills: ok — 1.0.0 (matches CLI)" in healthy.stdout, healthy.stdout
    source_manifest = json.loads((archive / "MANIFEST.json").read_text())
    (archive / "MANIFEST.json").write_text(json.dumps({**source_manifest, "version": "0.9.0"}))
    drift = doctor()
    assert drift.returncode == 5, (drift.stdout, drift.stderr)
    assert "skills: failed — 0.9.0 (drift from CLI 1.0.0)" in drift.stdout, drift.stdout
    (archive / "MANIFEST.json").write_text(json.dumps(source_manifest))
    print("PASS: doctor identifies matching and drifted skills versions")

    personal = home / ".codex/skills/my-skill"
    personal.mkdir()
    (personal / "SKILL.md").write_text("# personal\n")
    modified = home / ".codex/skills/adp-flow"
    original_marker = (modified / ".adp-provenance.json").read_text()
    (modified / "SKILL.md").write_text("# modified locally\n")
    refused = setup("codex")
    assert "Installed 0 ADP skills" in refused and str(modified) in refused, refused
    assert "Refusing unmarked or modified" in refused, refused
    assert (modified / "SKILL.md").read_text() == "# modified locally\n"
    assert (modified / ".adp-provenance.json").read_text() == original_marker
    print("PASS: setup refuses locally modified marked skills without changing provenance")

    resources = home / "personal-resources"
    resources.mkdir()
    (resources / "notes.txt").write_text("personal notes\n")
    linked = home / ".codex/skills/adp-plan"
    (linked / "my-resources").symlink_to(resources, target_is_directory=True)
    (claude / "missing-resource").symlink_to(home / "missing-resource")
    markers = {target: (target / ".adp-provenance.json").read_text() for target in (linked, claude)}
    (archive / "MANIFEST.json").write_text(json.dumps({**source_manifest, "version": "2.0.0"}))
    for client, target, link in (("codex", linked, "my-resources"), ("claude", claude, "missing-resource")):
        refused = setup(client)
        assert "Refusing unmarked or modified" in refused and str(target) in refused, refused
        assert (target / link).is_symlink(), f"refresh deleted {link}"
        assert (target / ".adp-provenance.json").read_text() == markers[target]
    assert (resources / "notes.txt").read_text() == "personal notes\n"
    print("PASS: version refresh preserves locally added directory and dangling symlinks")

    (config / "tokens.json").unlink()
    logout = subprocess.run(["bash", str(cli / "adp"), "tools", "logout"], env=env,
                            capture_output=True, text=True, timeout=30)
    assert logout.returncode == 0, logout.stderr
    assert "Removed 0 ADP skills" in logout.stderr, logout.stderr
    assert (linked / "my-resources").is_symlink()
    assert (claude / "missing-resource").is_symlink()
    assert str(linked) in logout.stderr and str(claude) in logout.stderr, logout.stderr
    assert (resources / "notes.txt").read_text() == "personal notes\n"
    assert (personal / "SKILL.md").read_text() == "# personal\n"
    assert (modified / "SKILL.md").read_text() == "# modified locally\n"
    assert str(modified) in logout.stderr, logout.stderr
    print("PASS: logout preserves directory and dangling symlinks, unmarked skills and edited files")
    (linked / "my-resources").unlink()
    (claude / "missing-resource").unlink()
    cleanup = subprocess.run(["bash", str(cli / "adp"), "tools", "logout"], env=env,
                             capture_output=True, text=True, timeout=30)
    assert cleanup.returncode == 0 and "Removed 2 ADP skills" in cleanup.stderr, cleanup.stderr
    assert not claude.exists() and not linked.exists()
    print("PASS: logout removes unchanged marked skills once local symlinks are removed")
    again = subprocess.run(["bash", str(cli / "adp"), "tools", "logout"], env=env,
                           capture_output=True, text=True, timeout=30)
    assert again.returncode == 0 and "Removed 0 ADP skills" in again.stderr, again.stderr
    assert (personal / "SKILL.md").read_text() == "# personal\n"
    assert (modified / "SKILL.md").read_text() == "# modified locally\n"
    print("PASS: repeated logout leaves personal and modified skills untouched")
PY
