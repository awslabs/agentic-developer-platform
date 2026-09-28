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
USER 1000:1000
