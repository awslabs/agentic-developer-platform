FROM 879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-superplane-skypilot@sha256:0a5115d0840164c9cf9b17d5c455322bda2c35f68902f28f91ef6d9dd84214c2 AS high-maintenance
FROM 879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-superplane-skypilot@sha256:46c383b2c236e48d58734e26a44da734a0ef6b7baf6cdb867430bfdbf4d92a86 AS rsync-security
FROM 879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-dev-agent-context-deepwiki@sha256:782f7587ec1a803231eb0fa38518975842d1748df09a6b20f179af1caedf823b AS curl-security
FROM 879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-superplane-skypilot@sha256:2ea72b3f76d6e2f8b584c9ec780811a14bc94dc983d94382bc751363bb59a020 AS openssh-security
# Official CPython patch release on the same Debian Trixie ABI. Keep SkyPilot's
# provider packages; do not copy the official image's pip/site-packages tree.
FROM public.ecr.aws/docker/library/python:3.10.21-slim@sha256:31dd4d9529d02d7436659061cb7564cd4733fc90e5e152709a942d53382ec8d0 AS python-security
RUN rm -rf /usr/local/lib/python3.10/site-packages

# Preserve the reviewed S03 SkyPilot 0.12.3 + setuptools artifact, then apply
# maintained distro and Python dependency fixes. Do not restart from :latest.
FROM 879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-superplane-skypilot@sha256:f8d893cfabadff1946f7ccb8e2c0ba002322d1bfb0ee416cf6ce039f45de55ec

USER root
# Keep kubectl within one minor of EKS 1.35 and replace its vulnerable Go runtime.
ADD --checksum=sha256:3cfeaf80be482b435b0aa214aff6e0b2c312ee23c0ff20810c75517b6004c6eb https://dl.k8s.io/release/v1.35.9/bin/linux/amd64/kubectl /usr/local/bin/kubectl
RUN chmod 0755 /usr/local/bin/kubectl && kubectl version --client=true
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get -y upgrade \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY requirements-security.txt /opt/adp-security/requirements-security.txt
RUN python -m pip install --no-cache-dir --index-url https://pypi.org/simple \
    -r /opt/adp-security/requirements-security.txt \
    && python -m pip check

# Replace the complete old stdlib, including obsolete files and bytecode, while
# retaining ABI-compatible cp310 third-party wheels from the provider environment.
RUN find /usr/local/lib/python3.10 -mindepth 1 -maxdepth 1 ! -name site-packages -exec rm -rf {} +
COPY --from=python-security /usr/local/bin/python3.10 /usr/local/bin/python3.10
COPY --from=python-security /usr/local/lib/libpython3.10.so.1.0 /usr/local/lib/libpython3.10.so.1.0
COPY --from=python-security /usr/local/lib/libpython3.so /usr/local/lib/libpython3.so
COPY --from=python-security /usr/local/lib/python3.10/ /usr/local/lib/python3.10/
COPY --from=python-security /usr/local/include/python3.10/ /usr/local/include/python3.10/
ENV PYTHON_VERSION=3.10.21 \
    PYTHON_SHA256=a0da1e72132e950154eca0f6f47d5db828454700de20e5113667940d81e0db04
RUN python -m pip check && python -c "import sys, ssl, sqlite3, bz2, lzma, ctypes; assert sys.version_info[:3] == (3, 10, 21)"

# Keep the upstream entrypoint/command and API behavior; deployment supplies
# the API process command. Health/compatibility tests must use this final image.
RUN python -c "import sky; import ssl; import OpenSSL; import cryptography; import anyio; assert sky.__version__ == '0.12.3'"

# Preserve all three SSH packages with the reviewed critical/high backports.
COPY --from=openssh-security /out/ /tmp/adp-openssh-packages/
RUN python - <<'PYTHON'
import grp, os, pwd, subprocess, shutil
from pathlib import Path
root = Path('/tmp/adp-openssh-packages')
packages = sorted(root.glob('openssh-*.deb'))
assert len(packages) == 3
existing = set(Path('/etc/ssh').glob('ssh_host_*'))
subprocess.run(['dpkg', '--force-confold', '-i', *map(str, packages)], check=True, env={**os.environ, 'DEBIAN_FRONTEND': 'noninteractive'})
for path in set(Path('/etc/ssh').glob('ssh_host_*')) - existing:
    path.unlink()
for name in ['openssh-client', 'openssh-server', 'openssh-sftp-server']:
    assert subprocess.check_output(['dpkg-query', '-W', '-f=${Version}', name], text=True) == '1:10.0p1-7+deb13u4+adp.security.2'
out = Path('/opt/adp-openssh-security')
out.mkdir(exist_ok=True, parents=True)
for name in ['source-lock.json', 'NOTICE', 'patched-source-sha256.txt']:
    shutil.copyfile(root / name, out / name)
shutil.copytree(root / 'source-authentication', out / 'source-authentication', dirs_exist_ok=True)
shutil.rmtree(root)
# Native SSH requires a passwd entry for the deployment's existing UID/GID1000.
try:
    pwd.getpwuid(1000)
except KeyError:
    try:
        grp.getgrgid(1000)
    except KeyError:
        subprocess.run(['groupadd', '--gid', '1000', 'sky'], check=True)
    subprocess.run(['useradd', '--uid', '1000', '--gid', '1000', '--create-home', '--home-dir', '/home/sky', '--shell', '/bin/sh', 'sky'], check=True)
PYTHON

# Apply after apt upgrades so distribution packages cannot replace the fix.
COPY --from=curl-security /out/ /tmp/adp-curl/
RUN dpkg -i /tmp/adp-curl/*.deb && ldconfig && rm -rf /tmp/adp-curl
COPY --from=curl-security /licenses/ /opt/adp-curl-security/

# Keep the reviewed rsync fix in the normal recipe as well as its derivative.
COPY --from=rsync-security /out/rsync.deb /tmp/adp-rsync.deb
RUN echo 'a216c57ce614e07105150235a9eee2612e7799856486e2917fc92c65f73d46d1  /tmp/adp-rsync.deb' | sha256sum -c - \
    && dpkg -i /tmp/adp-rsync.deb && rm /tmp/adp-rsync.deb
COPY --from=rsync-security /provenance/ /opt/adp-rsync-security/

# Apply the same offline, checksum-locked maintenance to every SkyPilot variant.
# The installer rejects unreviewed inputs and checks the full provider environment.
RUN --network=none --mount=type=bind,from=high-maintenance,source=/,target=/maintenance-input \
    python3 /maintenance-input/maintenance/install.py /maintenance-input
