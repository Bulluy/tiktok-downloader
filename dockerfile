# Dockerfile untuk deploy TikTok Downloader (Flask) ke Render / platform lain
# yang mendukung Docker. FFmpeg di-install di sini supaya mode Audio (MP3) jalan.

FROM python:3.12-slim

# Install FFmpeg (dibutuhkan untuk convert ke MP3)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Render otomatis mengisi variabel PORT saat container dijalankan
ENV PORT=5000
EXPOSE 5000

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --workers 2 --threads 4 --timeout 120 app:app"]
