# Dependency-only repair for an installed Superplane API or transport image.
# The operator supplies and verifies the exact base digest and original user.
ARG BASE
FROM ${BASE}
ARG BASE
ARG SECURITY_REVISION
LABEL io.adp.security.certifi.base="${BASE}" \
      io.adp.security.certifi.revision="${SECURITY_REVISION}"
USER root
ADD --checksum=sha256:e9b54de76e530bb6cf0ccd036787103acb58fd42ba930ea8bf948ddbaa2f61d1 https://files.pythonhosted.org/packages/32/d1/a3652fe2c19134d0d15922177821d863cbd7e842d53a21bbe7e8e3a1b5ac/certifi-2026.10.23-py3-none-any.whl /tmp/adp-certifi/certifi-2026.10.23-py3-none-any.whl
ADD --checksum=sha256:fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70 https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl /tmp/adp-certifi/pip-26.2.2-py3-none-any.whl
RUN --network=none python -m pip install --no-index --no-deps --no-cache-dir \
      /tmp/adp-certifi/certifi-2026.10.23-py3-none-any.whl \
      /tmp/adp-certifi/pip-26.2.2-py3-none-any.whl \
    && python -m pip check \
    && rm -rf /tmp/adp-certifi
# Use the repository root as the build context. pip can also consult the system
# store, so refreshing only its vendored bundle is insufficient on older bases.
COPY modules/domain-apps/superplane/images/skypilot/high-maintenance/distrust_system_root.py /opt/adp-system-ca-security/distrust_system_root.py
RUN --network=none python /opt/adp-system-ca-security/distrust_system_root.py
ARG RUNTIME_USER=65532:65532
USER ${RUNTIME_USER}
