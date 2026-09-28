#!/bin/sh
set -eu
cd /src
make check-progs -j2
./runtests.py --rsync-bin=/src/rsync --use-tcp -j2
# Run these ordinary-transfer cases with both peer version arrangements. The
# security regression suite above always uses the fixed binary at both ends.
./runtests.py --rsync-bin=/src/rsync --rsync-bin2=/out/rsync-debian-before -j2 acls chmod compress-options delete exclude hardlinks inplace relative xattrs
./runtests.py --rsync-bin=/out/rsync-debian-before --rsync-bin2=/src/rsync -j2 acls chmod compress-options delete exclude hardlinks inplace relative xattrs
