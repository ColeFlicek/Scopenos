FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    libpq-dev \
    nodejs \
    npm \
    curl \
    && curl -fsSL https://github.com/scip-code/scip/releases/download/v0.8.1/scip-linux-amd64.tar.gz \
       | tar -xz -C /usr/local/bin/ scip \
    && rm -rf /var/lib/apt/lists/*

# SCIP indexers — compiler-accurate call graphs (primary structural layer).
# scip-python: indexes Python source → binary .scip file
# scip CLI (installed above): converts binary .scip → JSON for ScipImporter
RUN npm install -g @sourcegraph/scip-python

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY schema_org.sql .
COPY scripts/bootstrap_api_key.py ./scripts/bootstrap_api_key.py

EXPOSE 3004

LABEL org.opencontainers.image.title="Scopenos" \
      org.opencontainers.image.description="Scopenos — call graph traversal, semantic search, and decision memory via MCP" \
      org.opencontainers.image.vendor="Scopenos" \
      org.opencontainers.image.source="https://github.com/ColeFlicek/Scopenos" \
      org.opencontainers.image.licenses="MIT"

CMD ["python", "-m", "src.server"]
