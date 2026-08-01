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
ENV MAX_CLIP_SECONDS=300 \
    CLIP_TTL_SECONDS=3600 \
    MAX_CONCURRENT_RENDERS=2

# Bound to 8000 deliberately: the Railway domain forwards to that port, and
# honouring the injected $PORT (8080) is what makes the proxy return 502.
EXPOSE 8000
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:8000", \
     "--workers", "2", "--threads", "4", "--timeout", "300"]
