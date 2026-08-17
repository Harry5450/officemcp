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
    curl -fsSL -o /usr/local/bin/officecli \
        https://github.com/iOfficeAI/OfficeCLI/releases/download/v1.0.144/officecli-linux-x64 && \
    chmod +x /usr/local/bin/officecli && \
    test -x /usr/local/bin/officecli && \
    /usr/local/bin/officecli --version

ENV PATH="/usr/local/bin:${PATH}"

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn httpx

COPY app.py .

RUN mkdir -p /app/output

EXPOSE 8080

CMD ["python", "app.py"]
