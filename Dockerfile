FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        libicu-dev \
        libssl3 \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://raw.githubusercontent.com/iOfficeAI/OfficeCLI/main/install.sh | bash

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn httpx

COPY app.py .

RUN mkdir -p /app/output

EXPOSE 8080

CMD ["python", "app.py"]
