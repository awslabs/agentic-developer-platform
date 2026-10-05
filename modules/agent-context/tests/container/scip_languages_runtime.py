"""Run installed Python/TypeScript/Go SCIP indexers on a tiny offline repository."""
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile

sys.path.insert(0, '/app')
from scip_indexer import index_repo  # noqa: E402
from scip_proto.scip_pb2 import Index  # noqa: E402


def main():
    if not __debug__:
        raise RuntimeError('Acceptance requires assertions')
    assert os.getuid() == os.getgid() == 10001
    os.environ['GOPROXY'] = 'off'
    os.environ['GOSUMDB'] = 'off'
    files = {
        'main.py': 'def greeting(name: str) -> str:\n    return "hello " + name\n',
        'main.ts': 'export function greeting(name: string): string { return "hello " + name; }\n',
        'main.go': 'package main\nfunc greeting(name string) string { return "hello " + name }\nfunc main() { println(greeting("fixture")) }\n',
        'go.mod': 'module example.invalid/fixture\ngo 1.26\n',
        'package.json': '{"name":"scip-runtime-fixture","version":"1.0.0","private":true}\n',
        'tsconfig.json': '{"compilerOptions":{"target":"ES2020"},"include":["main.ts"]}\n',
    }
    with tempfile.TemporaryDirectory(prefix='scip-languages-') as directory:
        root = Path(directory)
        for name, content in files.items():
            (root / name).write_text(content)
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(root), '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture'], check=True)
        report = index_repo(str(root), 'fixture/scip-languages', ['python', 'typescript', 'go'])
        results = {row.language: row for row in report.results}
        for language, filename in [('python', 'main.py'), ('typescript', 'main.ts'), ('go', 'main.go')]:
            row = results[language]
            assert row.success, repr(row)
            index = Index()
            index.ParseFromString(Path(row.scip_path).read_bytes())
            assert any(d.relative_path.endswith(filename) and d.occurrences for d in index.documents), language
        for name, content in files.items():
            assert (root / name).read_text() == content, name
    print(json.dumps({'indexers': sorted(results), 'actual_scip_documents_and_occurrences': 'passed',
                      'source_unchanged': True, 'network': 'disabled'}))


if __name__ == '__main__':
    main()
