FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    PARALLEL_DOWNLOADS=1 \
    QUEUE_CAPACITY=8
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py worker_download.py ./
# Your GitHub repository currently stores these assets at the root level.
RUN mkdir -p ./static
COPY index.html styles.css app.js favicon.svg robots.txt ./static/
RUN useradd -r -u 10001 appuser
USER appuser
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
