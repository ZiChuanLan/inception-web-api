FROM python:3.11-slim

WORKDIR /app

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install ONLY Chromium and its minimal Linux dependencies (excludes Firefox, WebKit, ffmpeg)
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY app.py .

ENV HOST=0.0.0.0
ENV PORT=8011
ENV ENGINE=browser

EXPOSE 8011

CMD ["python", "app.py"]
