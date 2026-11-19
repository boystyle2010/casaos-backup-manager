FROM python:3.15-slim

ARG TARGETARCH
ARG APP_VERSION=1.0.3
ARG RCLONE_VERSION=v1.76.0

LABEL org.opencontainers.image.title="CasaOS Backup Manager Pro" \
      org.opencontainers.image.description="Self-contained Backup and Recovery Manager for CasaOS" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.source="https://github.com/boystyle2010/casaos-backup-manager" \
      io.casaos.icon="https://raw.githubusercontent.com/boystyle2010/casaos-backup-manager/main/icon.png"

ENV APP_VERSION=${APP_VERSION}

RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    curl \
    unzip \
    tar \
    zstd \
    openssl \
    ca-certificates \
    && ARCH="${TARGETARCH:-$(dpkg --print-architecture)}" \
    && echo "Installing Rclone ${RCLONE_VERSION} for architecture: ${ARCH}" \
    && curl -fsSL "https://downloads.rclone.org/${RCLONE_VERSION}/rclone-${RCLONE_VERSION}-linux-${ARCH}.deb" -o /tmp/rclone.deb \
    && dpkg -i /tmp/rclone.deb \
    && rm -f /tmp/rclone.deb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY app/ /app/

EXPOSE 8095

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -sS -o /dev/null --max-time 5 http://127.0.0.1:8095/ || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8095"]
