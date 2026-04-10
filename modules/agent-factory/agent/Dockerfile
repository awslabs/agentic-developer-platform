# Multi-stage build for MCP Agent Mail
FROM node:22-alpine AS builder

# Create non-root user
RUN addgroup -g 10001 agentmail && \
    adduser -u 10001 -G agentmail -s /bin/sh -D agentmail

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
RUN addgroup -g 10001 agentmail && \
    adduser -u 10001 -G agentmail -s /bin/sh -D agentmail

# Install dumb-init for proper signal handling
RUN apk add --no-cache dumb-init

# Create directories with proper ownership
RUN mkdir -p /app /data && \
    chown -R agentmail:agentmail /app /data

# Set working directory
WORKDIR /app

# Switch to non-root user
USER agentmail:agentmail

# Copy built application from builder stage
COPY --from=builder --chown=agentmail:agentmail /app/dist ./dist
COPY --from=builder --chown=agentmail:agentmail /app/node_modules ./node_modules
COPY --from=builder --chown=agentmail:agentmail /app/package*.json ./

# Copy startup script
COPY --chown=agentmail:agentmail startup.sh ./startup.sh
RUN chmod +x startup.sh

# Expose port
EXPOSE 8765

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:8765/health || exit 1

# Use dumb-init for proper signal handling
ENTRYPOINT ["dumb-init", "--"]

# Set environment variables
ENV NODE_ENV=production \
    PORT=8765

# Start the application with health server
CMD ["./startup.sh"]