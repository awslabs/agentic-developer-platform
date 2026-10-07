# This fixture needs only Node and POSIX shell; no native application modules.
# Keep Node 22 while avoiding the obsolete Debian libc/Perl in the old fixture.
FROM public.ecr.aws/docker/library/node:22.23.3-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402
COPY --chmod=0555 acceptance /opt/adp-checks/acceptance
USER 65534:65534
