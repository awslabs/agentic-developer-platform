"""Apply the retained Next.js lock update to exact reviewed upstream files."""
import hashlib
import json
from pathlib import Path
import subprocess


def main():
    bundle = Path(__file__).parent
    manifest = json.loads((bundle / 'source-lock.json').read_text())
    for stage in ('before', 'after'):
        for name, expected in manifest['files'].items():
            actual = hashlib.sha256(Path(name).read_bytes()).hexdigest()
            if actual != expected[stage]:
                raise RuntimeError(f'Unexpected upstream {name} at {stage}; review required')
        if stage == 'before':
            subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-i',
                            str(bundle / 'next-15.5.24.patch')], check=True)


if __name__ == '__main__':
    main()
