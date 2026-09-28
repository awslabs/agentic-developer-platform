# Bounded qualification checks use only the Python standard library.
# tar and gzip implement the executor's verified source archive transport.
FROM public.ecr.aws/lambda/python:3.12@sha256:9d05e09dee76e344595bc017f9efd7359d353ee6f1018b17b722b048f6033ab8
RUN dnf install -y tar gzip && dnf clean all
COPY --chmod=0555 modules/tools/validation/isolation-infra/branch-slug /opt/adp-checks/branch-slug
USER 65534:65534
ENTRYPOINT ["/var/lang/bin/python3.12"]
