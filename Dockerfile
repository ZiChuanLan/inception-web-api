FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

WORKDIR /app

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV HOST=0.0.0.0
ENV PORT=8011
ENV ENGINE=browser

EXPOSE 8011

CMD ["python", "app.py"]
