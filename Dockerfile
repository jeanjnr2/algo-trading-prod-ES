FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && mkdir -p /usr/share/zoneinfo/US \
    && ln -sf ../America/Chicago /usr/share/zoneinfo/US/Central \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY src /app/src

ENV PYTHONPATH=/app/src
CMD ["python", "-u", "-m", "run_live"] 
