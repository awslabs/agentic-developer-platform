"""Configure test imports for url-analysis skill tests."""

import sys
from pathlib import Path

# Add the skill directory to sys.path so we can import modules directly
SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))
