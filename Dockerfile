FROM python:3.12-slim

ARG RCLONE_VERSION=current

RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    curl \
    unzip \
    tar \
    zstd \
    openssl \
    ca-certificates \
    && curl -fsSL https://downloads.rclone.org/${RCLONE_VERSION}/rclone-current-linux-amd64.deb -o /tmp/rclone.deb \
    && dpkg -i /tmp/rclone.deb \
    && rm -f /tmp/rclone.deb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY app/ /app/

EXPOSE 8095
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8095"]
