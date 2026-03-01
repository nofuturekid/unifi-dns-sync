FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/nofuturekid/unifi-dns-sync"
LABEL org.opencontainers.image.description="Automatically syncs Docker container DNS CNAME records to UniFi Gateway"
LABEL org.opencontainers.image.licenses="MIT"

WORKDIR /app

RUN pip install --no-cache-dir docker requests

COPY unifi_dns_sync.py .

CMD ["python", "-u", "unifi_dns_sync.py"]
