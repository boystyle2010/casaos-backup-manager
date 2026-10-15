FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    curl \
    unzip \
    tar \
    zstd \
    openssl \
    rclone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/

EXPOSE 8095
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8095"]
