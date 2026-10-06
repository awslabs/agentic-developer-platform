FROM 879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-superplane-skypilot@sha256:2ea72b3f76d6e2f8b584c9ec780811a14bc94dc983d94382bc751363bb59a020 AS openssh-security
FROM public.ecr.aws/docker/library/python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential pkgconf libpopt-dev libacl1-dev libxxhash-dev libzstd-dev \
    liblz4-dev libssl-dev libidn2-dev zlib1g-dev gnupg rsync fakeroot acl attr
ADD --checksum=sha256:c55f9c9dc10fb8bec397b399a0fdded53cc9a2d8e30891bb0d63724d25c37bef https://github.com/RsyncProject/rsync/releases/download/v3.5.1/rsync-3.5.1.tar.gz /tmp/rsync.tar.gz
COPY release-key.asc rsync-3.5.1.tar.gz.asc source-lock.json /provenance/
RUN install -d -m0700 /tmp/gnupg && gpg --homedir /tmp/gnupg --batch --import /provenance/release-key.asc && \
    gpg --homedir /tmp/gnupg --batch --status-fd 1 --verify /provenance/rsync-3.5.1.tar.gz.asc /tmp/rsync.tar.gz > /provenance/signature-status.txt && \
    grep -F '[GNUPG:] VALIDSIG 8EDF88F4731A1E8B45D67FBE6909546B2C52EC2D ' /provenance/signature-status.txt && \
    mkdir /src /out && tar -xzf /tmp/rsync.tar.gz --strip-components=1 -C /src && \
    cp /usr/bin/rsync /out/rsync-debian-before
WORKDIR /src
RUN CFLAGS="$(dpkg-buildflags --get CFLAGS)" CPPFLAGS="$(dpkg-buildflags --get CPPFLAGS)" LDFLAGS="$(dpkg-buildflags --get LDFLAGS)" \
    ./configure --prefix=/usr --sysconfdir=/etc --with-included-zlib=no && \
    make -j2 && make DESTDIR=/stage install
# Run upstream security regression and ordinary transfer tests as an unprivileged
# user, retaining real failure/skip output. Runtime test containers use no network.
RUN useradd --uid 1000 --create-home tester && chown -R tester:tester /src
COPY package.py /package.py
RUN cd /tmp && apt-get download rsync=3.4.1+ds1-5+deb13u4 && \
    dpkg-deb -R /tmp/rsync_*.deb /package && python /package.py
FROM 879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-superplane-skypilot@sha256:265b773bc187017b27c5b17373246bbbc0ff80982928cc1e73c5bd0acff7bf2c AS runtime
USER root
COPY --from=build /out/rsync_3.5.1-0+adp1_amd64.deb /tmp/adp-rsync.deb
RUN dpkg -i /tmp/adp-rsync.deb && rm /tmp/adp-rsync.deb
COPY --from=build /provenance/ /opt/adp-rsync-security/
COPY --from=build /src/COPYING /src/NEWS.md /opt/adp-rsync-security/
# Maintain the runtime's JWT verifier even when this recipe inherits an older pin.
ADD --checksum=sha256:ad0cef71c756a56e74863c2919cf0985f72decbcfcb550ee2f422e7c62b5eedc https://files.pythonhosted.org/packages/9c/97/672cb32ce0dfea44b740cb7b4f97038463b9cf7c0ead1aacf595572851d6/pyjwt-2.14.0-py3-none-any.whl /tmp/pyjwt-2.14.0-py3-none-any.whl
RUN --network=none python -m pip list --format=json > /tmp/jwt-before.json \
    && python -m pip install --no-index --no-deps --no-cache-dir /tmp/pyjwt-2.14.0-py3-none-any.whl \
    && python -m pip check \
    && python -c "import json,importlib.metadata as m; from pathlib import Path; before={d['name'].lower():d['version'] for d in json.loads(Path('/tmp/jwt-before.json').read_text())}; after={d.metadata['Name'].lower():d.version for d in m.distributions()}; assert after['pyjwt']=='2.14.0'; assert {k:v for k,v in before.items() if k!='pyjwt'}=={k:v for k,v in after.items() if k!='pyjwt'}; p=Path('/opt/adp-security/requirements-security.txt'); s=p.read_text(); assert 'PyJWT==2.13.0' in s or 'PyJWT==2.14.0' in s; p.write_text(s.replace('PyJWT==2.13.0','PyJWT==2.14.0'))" \
    && rm /tmp/jwt-before.json /tmp/pyjwt-2.14.0-py3-none-any.whl
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
USER 1000:1000
