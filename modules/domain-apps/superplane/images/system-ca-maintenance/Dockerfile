# Repository-root build context. BASE must be an exact reviewed installed image.
ARG BASE
FROM ${BASE}
ARG BASE
ARG SECURITY_REVISION
LABEL io.adp.security.system-ca.base="${BASE}" \
      io.adp.security.system-ca.revision="${SECURITY_REVISION}"
USER root
COPY modules/domain-apps/superplane/images/skypilot/high-maintenance/distrust_system_root.py /opt/adp-system-ca-security/distrust_system_root.py
RUN --network=none python /opt/adp-system-ca-security/distrust_system_root.py
ARG RUNTIME_USER
USER ${RUNTIME_USER}
