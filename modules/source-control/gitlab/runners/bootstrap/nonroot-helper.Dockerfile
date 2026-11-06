FROM gitlab/gitlab-runner-helper:x86_64-v19.5.2@sha256:072376a64c654169ef9de96b4b40740c10f1ef1a69f913644f56b25d1d894401
RUN install -d -m 0700 -o 10001 -g 10001 /builds /cache /gitlab-runner-cache-init
USER 10001:10001
