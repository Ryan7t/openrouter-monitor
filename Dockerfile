FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY openrouter_monitor ./openrouter_monitor
COPY config.example.yaml ./config.example.yaml

RUN mkdir -p /app/data

CMD ["python", "-m", "openrouter_monitor", "--config", "/app/config.yaml"]
