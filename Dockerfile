FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn httpx python-docx openpyxl python-pptx
COPY server.py .
RUN mkdir -p /app/output
EXPOSE 8080
CMD ["python", "server.py"]
