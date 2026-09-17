# Multi-stage build for optimal production image
FROM python:3.11-slim AS base

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=random \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UNSTRUCTURED_CACHE_DIR=/tmp/unstructured_cache \
    NLTK_DATA=/tmp/nltk_data

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser appuser

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create cache directories and change ownership
RUN mkdir -p /tmp/unstructured_cache /tmp/nltk_data && \
    chown -R appuser:appuser /app /tmp/unstructured_cache /tmp/nltk_data

# Switch to non-root user
USER appuser

# Expose port
EXPOSE 8001

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8001/health || exit 1

# Production command using Gunicorn with Uvicorn workers
# Benefits: robust process management, auto-restart crashed workers, production-grade signal handling
# Worker formula: (2 × CPU cores) + 1, using 4 workers as default
# --forwarded-allow-ips ensures proper handling behind reverse proxy (Nginx/Traefik)
CMD ["gunicorn", "app.main:app", \
    "-w", "4", \
    "-k", "uvicorn.workers.UvicornWorker", \
    "--bind", "0.0.0.0:8001", \
    "--forwarded-allow-ips", "*", \
    "--access-logfile", "-", \
    "--error-logfile", "-", \
    "--capture-output", \
    "--timeout", "120", \
    "--graceful-timeout", "30"]