FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py index.html ./

# upgrade yt-dlp on every start, so site fixes arrive without a rebuild
CMD ["sh", "-c", "pip install -q -U --no-cache-dir \"yt-dlp[default]\" >/dev/null 2>&1; uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000} --proxy-headers"]
