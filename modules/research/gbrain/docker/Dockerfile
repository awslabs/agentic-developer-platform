FROM public.ecr.aws/oven/bun:1 AS build
WORKDIR /app
RUN bun install -g github:garrytan/gbrain@v0.42.26.0

FROM public.ecr.aws/oven/bun:1
WORKDIR /app

# Copy gbrain from global install
COPY --from=build /root/.bun/install/global/node_modules /app/node_modules
COPY --from=build /root/.bun/bin/gbrain /usr/local/bin/gbrain

# Copy configuration
COPY config/ /app/config/

# Install curl for health checks
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD curl -f http://localhost:3000/health || exit 1

CMD ["gbrain", "serve", "--http", "--port", "3000"]
