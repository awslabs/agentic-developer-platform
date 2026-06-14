# Context MCP Server — verb surface for the Knowledge layer (:5100)
# Issue: #1417
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code (staged into build dir by bs-agent-context-image.yml pre_build)
COPY door/ ./door/
COPY personal_context/ ./personal_context/

# Expose MCP port
EXPOSE 5100

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:5100/health || exit 1

# Run the server
CMD ["uvicorn", "door.server:app", "--host", "0.0.0.0", "--port", "5100"]
