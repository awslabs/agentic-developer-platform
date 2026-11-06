"""Restore only through a separately admitted writable helper pod."""

import os
import sys

from crew_checkpoint_helper import run_restore

if __name__ == "__main__":
    try:
        run_restore(os.environ)
    except Exception as error:
        print(f"checkpoint restore failed: {type(error).__name__}", file=sys.stderr)
        sys.exit(1)
