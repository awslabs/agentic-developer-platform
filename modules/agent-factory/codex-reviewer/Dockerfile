FROM public.ecr.aws/docker/library/node:24-bookworm-slim AS build

WORKDIR /build
COPY modules/agent-factory/codex-reviewer/package.json modules/agent-factory/codex-reviewer/package-lock.json ./
RUN npm ci
COPY modules/agent-factory/codex-reviewer/tsconfig.json ./
COPY modules/agent-factory/codex-reviewer/src ./src
RUN npm run build && npm prune --omit=dev

FROM public.ecr.aws/docker/library/node:24-bookworm-slim

LABEL org.opencontainers.image.title="agent-codex-reviewer"
LABEL org.opencontainers.image.description="Independent Codex SDK pull-request reviewer"

ENV DEBIAN_FRONTEND=noninteractive \
    HOME=/home/reviewer \
    NODE_ENV=production

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      build-essential ca-certificates curl git jq python3 python3-pip ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin reviewer

WORKDIR /app
COPY --from=build /build/dist ./dist
COPY --from=build /build/node_modules ./node_modules
COPY --from=build /build/package.json ./package.json
COPY modules/agent-factory/codex-reviewer/prompts ./prompts
COPY modules/agent-factory/codex-reviewer/codex-config.toml /home/reviewer/.codex/config.toml
RUN chown -R reviewer:reviewer /app /home/reviewer

USER reviewer
ENTRYPOINT ["node", "/app/dist/index.js"]
