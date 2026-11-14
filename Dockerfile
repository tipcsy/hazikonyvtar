FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    TZ=Europe/Budapest

# libzbar0: vonalkód olvasás, tesseract: szövegfelismerés a borítóról
RUN apt-get update \
 && apt-get install -y --no-install-recommends libzbar0 tesseract-ocr tesseract-ocr-hun tesseract-ocr-eng tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)" || exit 1

# Egy worker: az SQLite írások így egy folyamaton belül sorba rendezettek.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
