FROM python:3.11-slim

WORKDIR /app

# ffmpeg does the cutting and encoding; yt-dlp resolves the media URLs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p output/clips

# yt-dlp breaks whenever YouTube changes something, so requirements pin it to
# "latest at build time" rather than a version — redeploy to update it.
ENV PORT=8000 \
    MAX_CLIP_SECONDS=300 \
    CLIP_TTL_SECONDS=3600 \
    MAX_CONCURRENT_RENDERS=2

CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 300
