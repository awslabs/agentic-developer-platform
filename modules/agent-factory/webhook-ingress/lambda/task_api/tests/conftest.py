"""Import paths and default environment for Task API ingress tests."""

import os
import sys
from pathlib import Path

# lambda/ -> `import task_api.*`, `import common.*`
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
