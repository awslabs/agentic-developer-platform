import json
import pathlib
import subprocess
import tempfile

root = pathlib.Path(tempfile.mkdtemp(prefix="indexer-smoke-"))
out = {}


def run(args, cwd):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=180, check=False)
    if p.returncode:
        raise RuntimeError(str(args) + "\n" + p.stdout + "\n" + p.stderr)
    return p.stdout


py = root / "python"
py.mkdir()
(py / "fixture.py").write_text('def hello(name: str) -> str:\n    return "hello " + name\n')
(py / "pyproject.toml").write_text(
    '[project]\nname="security-fixture"\nversion="1.0.0"\n[tool.pyright]\ninclude=["fixture.py"]\n'
)
run(
    [
        "scip-python",
        "index",
        "--project-name",
        "security-fixture",
        "--project-version",
        "1.0.0",
        "--output",
        str(py / "index.scip"),
        str(py),
    ],
    py,
)
assert (py / "index.scip").stat().st_size > 100
out["scip-python"] = (py / "index.scip").stat().st_size
ts = root / "typescript"
ts.mkdir()
(ts / "package.json").write_text('{"name":"security-fixture","version":"1.0.0"}')
(ts / "tsconfig.json").write_text(
    '{"compilerOptions":{"target":"es2020","strict":true},"include":["fixture.ts"]}'
)
(ts / "fixture.ts").write_text(
    'export function hello(name: string): string { return "hello " + name; }\n'
)
run(["scip-typescript", "index"], ts)
assert (ts / "index.scip").stat().st_size > 100
out["scip-typescript"] = (ts / "index.scip").stat().st_size
go = root / "go"
go.mkdir()
(go / "go.mod").write_text("module example.test/fixture\n\ngo 1.26.0\n")
(go / "main.go").write_text('package fixture\nfunc Hello() string { return "hi" }\n')
run(["scip-go", "index"], go)
assert (go / "index.scip").stat().st_size > 100
out["scip-go"] = (go / "index.scip").stat().st_size
sbom = json.loads(run(["syft", "file:/usr/local/bin/zoekt-git-index", "-o", "json"], ts))
assert any(a["name"] == "stdlib" and a["version"] == "go1.26.8" for a in sbom["artifacts"])
out["syft_go_runtime_detected"] = True
result = json.loads(
    run(["trivy", "fs", "--scanners", "secret", "--no-progress", "--format", "json", str(ts)], ts)
)
assert result["SchemaVersion"] == 2
out["trivy_offline_scan"] = True
out["node"] = run(["node", "--version"], root).strip()
out["trivy"] = run(["trivy", "--version"], root).strip()
print(json.dumps(out))
