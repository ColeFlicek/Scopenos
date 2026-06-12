FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

# Install LSP servers for TypeScript/JS and Python type checking
RUN npm install -g typescript-language-server typescript 2>/dev/null || true
RUN pip install --no-cache-dir pyright || true

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

EXPOSE 3004

LABEL org.opencontainers.image.title="ACIP" \
      org.opencontainers.image.description="AI Code Intelligence Platform — call graph traversal, semantic search, and decision memory via MCP" \
      org.opencontainers.image.vendor="ACIP" \
      org.opencontainers.image.source="https://github.com/ColeFlicek/ACIP" \
      org.opencontainers.image.licenses="MIT"

CMD ["python", "-m", "src.server"]
