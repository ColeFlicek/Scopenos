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
COPY schema.sql .

EXPOSE 3004

LABEL org.opencontainers.image.title="ACIP" \
      org.opencontainers.image.description="AI Code Intelligence Platform — call graph traversal, semantic search, and decision memory via MCP" \
      org.opencontainers.image.vendor="ACIP" \
      org.opencontainers.image.source="https://github.com/ColeFlicek/ACIP" \
      org.opencontainers.image.licenses="MIT"

CMD ["python", "-m", "src.server"]
