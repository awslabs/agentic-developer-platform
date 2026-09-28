#!/usr/bin/env bash
set -euo pipefail
git clone https://github.com/nltk/nltk.git nltk-security-source
cd nltk-security-source
git checkout 574270e2ad368c8816976e584da56ddfb3fefbad
printf '3.10.3+adp1\n' > nltk/VERSION
python -m pip wheel --no-deps --wheel-dir ../packages .
