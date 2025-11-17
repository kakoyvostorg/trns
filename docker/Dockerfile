# Multi-stage Dockerfile for TRNS
FROM python:3.11-slim as builder

# Install build dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Production stage
FROM python:3.11-slim

# Install runtime dependencies (FFmpeg for audio processing)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy Python packages from builder
COPY --from=builder /root/.local /root/.local

# Make sure scripts in .local are usable
ENV PATH=/root/.local/bin:$PATH

# Create app directory
WORKDIR /app

# Copy application code
COPY src/ ./src/
COPY pyproject.toml ./
COPY requirements.txt ./

# Install the package in development mode (or use pip install . for production)
RUN pip install --no-cache-dir -e .

# Create directories for config and data
RUN mkdir -p /app/config /app/data

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:8000/health')" || exit 1

# Default command (can be overridden)
# Note: Use python -m uvicorn trns.bot.server:app for production
CMD ["python", "-c", "from trns.bot.server import main; main()"]

