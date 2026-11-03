FROM python:3.11-slim

WORKDIR /app

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Install native Debian Chromium + runtime fonts & ca-certificates
# Clean apt archives immediately to prevent deb bloat in image layers
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    fonts-liberation \
    ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* /tmp/* /root/.cache

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV HOST=0.0.0.0
ENV PORT=8011
ENV ENGINE=browser
ENV CHROME_PATH=/usr/bin/chromium

EXPOSE 8011

CMD ["python", "app.py"]
