# syntax=docker/dockerfile:1.7

# Stage 1: builder
FROM python:3.14-slim AS builder
WORKDIR /app
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*
COPY agent/requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Stage 2: runtime
FROM python:3.14-slim
WORKDIR /app

# Non-root user
RUN groupadd --gid 1000 kagent \
    && useradd --uid 1000 --gid kagent --shell /bin/bash --create-home kagent

# Copy installed deps and the agent package as a package (preserves imports).
COPY --from=builder /root/.local /home/kagent/.local
COPY agent/ ./agent/
RUN chown -R kagent:kagent /app /home/kagent

USER kagent

ENV PATH=/home/kagent/.local/bin:$PATH \
    PYTHONPATH=/app \
    PORT=8000 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["python", "-m", "agent.main"]
