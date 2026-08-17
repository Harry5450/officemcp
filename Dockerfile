FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        libicu-dev \
        libssl3 \
    && rm -rf /var/lib/apt/lists/*

ENV DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1

RUN set -euxo pipefail && \
    curl -fsSL https://raw.githubusercontent.com/iOfficeAI/OfficeCLI/main/install.sh | bash ; \
    echo "=== officecli location ===" && \
    (find / -name officecli -type f 2>/dev/null || true) && \
    echo "=== HOME=$HOME ==="

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn httpx

COPY app.py .

RUN mkdir -p /app/output

EXPOSE 8080

CMD ["python", "app.py"]
