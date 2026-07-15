FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends jq ca-certificates tzdata \
    && mkdir -p /usr/share/zoneinfo/US \
    && ln -sf ../America/Chicago /usr/share/zoneinfo/US/Central \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY src /app/src
COPY control /app/control
COPY scripts/algoctl /usr/local/bin/algoctl
RUN chmod +x /usr/local/bin/algoctl

ENV PYTHONPATH=/app/src
CMD ["python", "-u", "-m", "run_live", "--control", "/app/control/v8_es.json"]
