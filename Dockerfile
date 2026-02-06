FROM python:3.12-slim

WORKDIR /app

# Install system dependencies (gcc for C extensions, curl for uv installer)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv (fast Python package manager)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install Python dependencies (uv resolves from pyproject.toml + uv.lock)
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev --no-install-project || uv sync --no-dev --no-install-project

# Copy application code
COPY src/ ./src/

# Set Python path
ENV PYTHONPATH=/app/src

# Default command: start the triage engine + web UI
CMD ["uv", "run", "python", "-m", "assistant", "serve"]

# Expose the review UI port
EXPOSE 8080
