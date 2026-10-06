FROM public.ecr.aws/docker/library/python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS openssh-builder
RUN sed -i 's/^Types: deb$/Types: deb deb-src/' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::AllowInsecureRepositories=false update \
    && apt-get -y --no-install-recommends install dpkg-dev git gpgv \
    && apt-get -y --no-install-recommends -Ppkg.openssh.nognome,noudeb build-dep openssh=1:10.0p1-7+deb13u4
WORKDIR /src
RUN apt-get --download-only source openssh=1:10.0p1-7+deb13u4 \
    && mkdir -p /out/source-authentication \
    && cp /var/lib/apt/lists/*InRelease /out/source-authentication/ \
    && sha256sum /var/lib/apt/lists/*Sources* /usr/share/keyrings/debian-archive-keyring.gpg > /out/source-authentication/metadata-sha256.txt \
    && apt-cache showsrc openssh > /out/source-authentication/apt-showsrc.txt
COPY modules/agent-context/images/deepwiki/security/openssh/ /build-tools/
RUN python /build-tools/authenticate-source.py && python /build-tools/prepare.py
RUN --network=none sh /build-tools/build.sh

FROM 879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-superplane-skypilot@sha256:5fa72c08cf9fb618406b3543fe4d4aed4e1f0c596383cb2a1beb97de09cf0999 AS runtime
USER root
COPY --from=openssh-builder /out/openssh-client_10.0p1-7+deb13u4+adp.security.1_amd64.deb /tmp/adp-openssh/client.deb
COPY --from=openssh-builder /src/openssh-server_10.0p1-7+deb13u4+adp.security.1_amd64.deb /tmp/adp-openssh/server.deb
COPY --from=openssh-builder /src/openssh-sftp-server_10.0p1-7+deb13u4+adp.security.1_amd64.deb /tmp/adp-openssh/sftp-server.deb
COPY platform/security/skypilot-openssh/install.py /tmp/adp-openssh/install.py
RUN python /tmp/adp-openssh/install.py
COPY --from=openssh-builder /out/source-lock.json /out/NOTICE /out/patched-source-sha256.txt /opt/adp-openssh-security/
COPY --from=openssh-builder /out/source-authentication/ /opt/adp-openssh-security/source-authentication/
# Maintain the runtime's JWT verifier even when this recipe inherits an older pin.
ADD --checksum=sha256:ad0cef71c756a56e74863c2919cf0985f72decbcfcb550ee2f422e7c62b5eedc https://files.pythonhosted.org/packages/9c/97/672cb32ce0dfea44b740cb7b4f97038463b9cf7c0ead1aacf595572851d6/pyjwt-2.14.0-py3-none-any.whl /tmp/pyjwt-2.14.0-py3-none-any.whl
RUN --network=none python -m pip list --format=json > /tmp/jwt-before.json \
    && python -m pip install --no-index --no-deps --no-cache-dir /tmp/pyjwt-2.14.0-py3-none-any.whl \
    && python -m pip check \
    && python -c "import json,importlib.metadata as m; from pathlib import Path; before={d['name'].lower():d['version'] for d in json.loads(Path('/tmp/jwt-before.json').read_text())}; after={d.metadata['Name'].lower():d.version for d in m.distributions()}; assert after['pyjwt']=='2.14.0'; assert {k:v for k,v in before.items() if k!='pyjwt'}=={k:v for k,v in after.items() if k!='pyjwt'}; p=Path('/opt/adp-security/requirements-security.txt'); s=p.read_text(); assert 'PyJWT==2.13.0' in s or 'PyJWT==2.14.0' in s; p.write_text(s.replace('PyJWT==2.13.0','PyJWT==2.14.0'))" \
    && rm /tmp/jwt-before.json /tmp/pyjwt-2.14.0-py3-none-any.whl
USER 1000:1000
