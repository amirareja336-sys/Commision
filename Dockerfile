# Reconciliation Console — container image
#
# Includes Playwright + Chromium (needed by the "Fetch from Abronal"
# scraper) baked in at build time, so no --with-deps step is needed
# at container startup.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System libraries Playwright's Chromium needs at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
        libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
        libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 \
        fonts-liberation wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt \
    && playwright install chromium

COPY . .

# Writable at runtime even if the image itself is read-only-mounted;
# actual persistence for these paths is handled by the volumes in
# docker-compose.yml, this just guarantees they exist on first boot.
RUN mkdir -p /app/data/uploads/sot /app/data/uploads/abronal /app/exports /app/db

EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
