#!/usr/bin/env python3
"""Re-read Product and executor observations before emitting qualification."""

from qualification_transform_release import main


if __name__ == "__main__":
    main(emitter=True)
