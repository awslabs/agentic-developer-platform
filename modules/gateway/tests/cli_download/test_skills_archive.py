import hashlib
import importlib.util
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.cli_download.routes import ALLOWED_SCRIPTS, router

GATEWAY = Path(__file__).resolve().parents[2]
BUILDER = GATEWAY / "scripts/build-skills-archive.sh"
ADP = GATEWAY / "cli/adp"


def test_smoke_build_stages_skills_from_clean_checkout(tmp_path):
    buildspec = yaml.safe_load((GATEWAY.parents[1] / "codebuild/bs-gateway-smoke.yml").read_text())
    commands = buildspec["phases"]["build"]["commands"]
    stage = "bash modules/gateway/scripts/build-skills-archive.sh --stage"
    assert stage in commands
    assert commands.index(stage) < commands.index("cd modules/gateway")
    assert commands.index("cd modules/gateway") < commands.index("docker build --pull -t adp-gateway:pr-smoke .")

    gateway = tmp_path / "modules/gateway"
    (gateway / "scripts").mkdir(parents=True)
    shutil.copyfile(BUILDER, gateway / "scripts/build-skills-archive.sh")
    source = tmp_path / "modules/agent-factory/skills"
    (source / "adp-flow").mkdir(parents=True)
    manifest = {"schema_version": 1, "skills": [{"name": "adp-flow", "clients": ["codex"], "invocation": "adp flow"}]}
    (source / "MANIFEST.json").write_text(json.dumps(manifest))
    (source / "adp-flow/SKILL.md").write_text("# Example flow\n")
    (source / "adp-flow/scripts").mkdir()
    (source / "adp-flow/scripts/plan_writer.py").write_text("print('example')\n")
    (source / "adp-flow/LICENSE").write_text("Example licence\n")
    staged = gateway / "skills-source"
    assert not staged.exists()
    result = subprocess.run(stage.split(), cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads((staged / "MANIFEST.json").read_text()) == manifest
    assert (staged / "adp-flow/SKILL.md").read_text() == "# Example flow\n"
    assert (staged / "adp-flow/scripts/plan_writer.py").read_text() == "print('example')\n"
    assert (staged / "adp-flow/LICENSE").read_text() == "Example licence\n"
    result = build(staged, tmp_path / "output")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "output/adp-skills.tar.gz").is_file()


def build(source, destination):
    return subprocess.run(["bash", str(BUILDER), "--build", str(source), str(destination), str(ADP)], capture_output=True, text=True, timeout=20)


def test_archive_build_and_gateway_routes(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    manifest = {"schema_version": 1, "skills": [{"name": "adp-flow", "clients": ["claude-code", "codex"], "invocation": "adp flow"}]}
    (source / "MANIFEST.json").write_text(json.dumps(manifest))
    skill = source / "adp-flow"
    (skill / "agents").mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Example flow\n")
    (skill / "agents/openai.yaml").write_text("interface: {}\n")
    (source / "_shared").mkdir()
    (source / "_shared/reference.md").write_text("# Shared\n")
    output = tmp_path / "output"
    result = build(source, output)
    assert result.returncode == 0, result.stderr

    archive_file = output / "adp-skills.tar.gz"
    archive_bytes = archive_file.read_bytes()
    checksum_bytes = (output / "adp-skills.tar.gz.sha256").read_bytes()
    expected = hashlib.sha256(archive_bytes).hexdigest()
    assert checksum_bytes == f"{expected}  adp-skills.tar.gz\n".encode()
    assert len(archive_bytes) <= 1024 * 1024
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
        names = archive.getnames()
        assert names == ["MANIFEST.json", "skills/adp-flow/SKILL.md", "skills/adp-flow/agents/openai.yaml", "_shared/reference.md"]
        packed = json.load(archive.extractfile("MANIFEST.json"))
        assert packed["version"] == "1.0.0"
        assert packed["skills"] == manifest["skills"]

    monkeypatch.setitem(ALLOWED_SCRIPTS, "adp-skills.tar.gz", archive_file)
    monkeypatch.setitem(ALLOWED_SCRIPTS, "adp-skills.tar.gz.sha256", output / "adp-skills.tar.gz.sha256")
    app = FastAPI()
    app.include_router(router, prefix="/api")
    client = TestClient(app)
    for name, body, media in (
        ("adp-skills.tar.gz", archive_bytes, "application/gzip"),
        ("adp-skills.tar.gz.sha256", checksum_bytes, "text/plain"),
    ):
        response = client.get("/api/cli/" + name)
        assert response.status_code == 200
        assert response.content == body
        assert response.headers["content-type"].startswith(media)
    assert client.get("/api/cli/adp-skills-nope.tar.gz").status_code == 404


def test_builder_rejects_invalid_skill_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "MANIFEST.json").write_text(
        json.dumps({"schema_version": 1, "skills": [{"name": "../escape", "clients": ["codex"], "invocation": "adp flow"}]})
    )
    result = build(source, tmp_path / "output")
    assert result.returncode != 0
    assert "invalid skills manifest entry" in result.stderr
    assert not (tmp_path / "output/adp-skills.tar.gz").exists()


def test_packaged_archive_served_from_image_layout(tmp_path):
    source = tmp_path / "skills-source"
    source.mkdir()
    (source / "MANIFEST.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills": [{"name": "adp-flow", "clients": ["codex"], "invocation": "adp flow"}],
            }
        )
    )
    (source / "adp-flow").mkdir()
    (source / "adp-flow/SKILL.md").write_text("# ADP flow\n")
    output = tmp_path / "generated-skills"
    result = build(source, output)
    assert result.returncode == 0, result.stderr

    gateway = tmp_path / "gateway"
    cli = gateway / "cli"
    cli.mkdir(parents=True)
    for name in ("adp-skills.tar.gz", "adp-skills.tar.gz.sha256"):
        shutil.copyfile(output / name, cli / name)
    route_file = gateway / "src/cli_download/routes.py"
    route_file.parent.mkdir(parents=True)
    shutil.copyfile(GATEWAY / "src/cli_download/routes.py", route_file)
    spec = importlib.util.spec_from_file_location("packaged_cli_routes", route_file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.ALLOWED_SCRIPTS["adp-skills.tar.gz"] == cli / "adp-skills.tar.gz"
    assert module.ALLOWED_SCRIPTS["adp-skills.tar.gz.sha256"] == cli / "adp-skills.tar.gz.sha256"
    assert ("COPY --from=builder /app/generated-skills/adp-skills.tar.gz /app/generated-skills/adp-skills.tar.gz.sha256 cli/") in (
        GATEWAY / "Dockerfile"
    ).read_text()

    app = FastAPI()
    app.include_router(module.router, prefix="/api")
    with TestClient(app) as client:
        archive = client.get("/api/cli/adp-skills.tar.gz")
        checksum = client.get("/api/cli/adp-skills.tar.gz.sha256")
        assert archive.status_code == checksum.status_code == 200
        digest = hashlib.sha256(archive.content).hexdigest()
        assert checksum.text == f"{digest}  adp-skills.tar.gz\n"
        with tarfile.open(fileobj=io.BytesIO(archive.content), mode="r:gz") as package:
            assert json.load(package.extractfile("MANIFEST.json"))["version"] == "1.0.0"
            assert package.extractfile("skills/adp-flow/SKILL.md").read() == b"# ADP flow\n"

        (cli / "adp-skills.tar.gz.sha256").unlink()
        assert client.get("/api/cli/adp-skills.tar.gz.sha256").status_code == 404


def test_shipped_skills_archive_includes_scripts_templates_and_license(tmp_path):
    source = GATEWAY.parents[0] / "agent-factory/skills"
    result = build(source, tmp_path / "output")
    assert result.returncode == 0, result.stderr
    with tarfile.open(tmp_path / "output/adp-skills.tar.gz") as archive:
        names = set(archive.getnames())
        assert {
            "skills/adp-flow/SKILL.md",
            "skills/adp-flow/agents/openai.yaml",
            "skills/adp-flow/scripts/plan_writer.py",
            "skills/adp-flow/scripts/publish.py",
            "skills/adp-flow/checks.md",
            "skills/adp-flow/intent.template.md",
            "skills/adp-grilling/SKILL.md",
            "skills/adp-grilling/agents/openai.yaml",
            "skills/adp-grilling/LICENSE",
            "skills/adp-grilling/NOTICE",
        } <= names
        assert not any("/tests/" in name or "__pycache__" in name for name in names)


def test_shared_tree_never_ships_caches_or_tests(tmp_path):
    """A test that imported a _shared script leaves __pycache__ behind; the archive must skip it."""
    source = tmp_path / "source"
    (source / "adp-flow" / "agents").mkdir(parents=True)
    (source / "MANIFEST.json").write_text(
        json.dumps({"schema_version": 1, "skills": [{"name": "adp-flow", "clients": ["codex"], "invocation": "adp flow"}]})
    )
    (source / "adp-flow/SKILL.md").write_text("# flow\n")
    (source / "adp-flow/agents/openai.yaml").write_text("interface: {}\n")
    (source / "_shared" / "__pycache__").mkdir(parents=True)
    (source / "_shared" / "tests").mkdir()
    (source / "_shared/check_proposal.py").write_text("print('ok')\n")
    (source / "_shared/__pycache__/check_proposal.cpython-312.pyc").write_bytes(b"\x00")
    (source / "_shared/tests/test_check.py").write_text("def test(): pass\n")
    result = build(source, tmp_path / "output")
    assert result.returncode == 0, result.stderr
    with tarfile.open(tmp_path / "output/adp-skills.tar.gz") as archive:
        names = set(archive.getnames())
    assert "_shared/check_proposal.py" in names
    assert not any("__pycache__" in name or "/tests/" in name for name in names), sorted(names)


def test_archive_packs_authoring_guide_and_templates_beside_shared(tmp_path):
    """An installed client must find the issue-authoring guide and templates in _shared/authoring/.

    Found live on 2026-11-03: the first adp-flow run reported "shared bundle is
    incomplete" because only policy.default.json and check_proposal.py were packed.
    """
    source = tmp_path / "skills"
    (source / "adp-flow").mkdir(parents=True)
    manifest = {"schema_version": 1, "skills": [{"name": "adp-flow", "clients": ["claude-code"], "invocation": "adp flow"}]}
    (source / "MANIFEST.json").write_text(json.dumps(manifest))
    (source / "adp-flow/SKILL.md").write_text("# Example\n")
    (source / "_shared").mkdir()
    (source / "_shared/policy.default.json").write_text("{}\n")
    rules = tmp_path / "rules"
    for relative, body in {
        "agents/issue-authoring.md": "# guide\n",
        "templates/epic-issue.md": "# epic\n",
        "templates/developer-issue.md": "# story\n",
        "templates/delivery-loop/evaluation-template.md": "# eval\n",
        "templates/delivery-loop/defect-template.md": "# defect\n",
    }.items():
        (rules / relative).parent.mkdir(parents=True, exist_ok=True)
        (rules / relative).write_text(body)
    adp = tmp_path / "adp"
    adp.write_text('readonly ADP_VERSION="1.0.0"\n')
    script = Path(__file__).resolve().parents[2] / "scripts/build-skills-archive.sh"
    result = subprocess.run(["bash", str(script), "--build", str(source), str(tmp_path / "output"), str(adp)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    with tarfile.open(tmp_path / "output/adp-skills.tar.gz") as archive:
        names = archive.getnames()
    for expected in (
        "_shared/authoring/issue-authoring.md",
        "_shared/authoring/epic-issue.md",
        "_shared/authoring/developer-issue.md",
        "_shared/authoring/evaluation-template.md",
        "_shared/authoring/defect-template.md",
    ):
        assert expected in names, names
