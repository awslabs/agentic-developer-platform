# Multi-stage build for ADP Agent (GitHub + Chat entrypoints)
FROM node:22-alpine AS builder

# Create non-root user
RUN addgroup -g 10001 agent && \
    adduser -u 10001 -G agent -s /bin/sh -D agent

# Set working directory
WORKDIR /app

# Copy package files
COPY package*.json ./

# Install dependencies
RUN npm ci --only=production && npm cache clean --force

# Copy source code
COPY . .

# Build the application
RUN npm run build

# Production stage
FROM node:22-alpine AS production

# Create non-root user
RUN addgroup -g 10001 agent && \
    adduser -u 10001 -G agent -s /bin/sh -D agent

# Install dumb-init for proper signal handling
RUN apk add --no-cache dumb-init

# Create directories with proper ownership
RUN mkdir -p /app /data /tmp/workspace && \
    chown -R agent:agent /app /data /tmp/workspace

# Set working directory
WORKDIR /app

# Switch to non-root user
USER agent:agent

# Copy built application from builder stage
COPY --from=builder --chown=agent:agent /app/dist ./dist
COPY --from=builder --chown=agent:agent /app/node_modules ./node_modules
COPY --from=builder --chown=agent:agent /app/package*.json ./

# Copy personas from repo (baked into image for zero-latency cold start)
COPY --chown=agent:agent ../rules/personas/ ./personas/

# Copy startup script
COPY --chown=agent:agent startup.sh ./startup.sh
RUN chmod +x startup.sh

# Expose port
EXPOSE 8765

# Health check (only active for github entrypoint; chat agent exits after processing)
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:8765/health || exit 1

# Use dumb-init for proper signal handling
ENTRYPOINT ["dumb-init", "--"]

# Set environment variables
ENV NODE_ENV=production \
    PORT=8765 \
    AGENT_ENTRYPOINT=github \
    PERSONAS_DIR=/app/personas

# Start the application (dispatched by AGENT_ENTRYPOINT)
CMD ["./startup.sh"]