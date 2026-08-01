"""YouTube clip tool: cut a section of a video and burn in a channel credit card."""

import io
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid

import requests
from PIL import Image, ImageDraw, ImageFont

CLIP_DIR = os.path.join(os.path.dirname(__file__), "output", "clips")

FONT_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
FONT_REG = "/System/Library/Fonts/Supplemental/Arial.ttf"

FIT_MODES = ("stretch", "zoom", "bars")

POSITIONS = ("top-left", "top-right", "bottom-left", "bottom-right")

RESOLUTION_PRESETS = (2160, 1440, 1080, 720, 480, 360)
ASPECT_PRESETS = ("16:9", "9:16", "1:1", "4:5", "4:3", "21:9")

MIN_RESOLUTION, MAX_RESOLUTION = 144, 4320

# Public deployments need a ceiling: every clip costs CPU and bandwidth.
MAX_CLIP_SECONDS = float(os.environ.get("MAX_CLIP_SECONDS", 300))
CLIP_TTL_SECONDS = float(os.environ.get("CLIP_TTL_SECONDS", 3600))


def prune_clips(ttl=None):
    """Delete rendered clips older than the TTL so the disk can't fill up."""
    ttl = CLIP_TTL_SECONDS if ttl is None else ttl
    if not os.path.isdir(CLIP_DIR) or ttl <= 0:
        return 0
    cutoff, removed = time.time() - ttl, 0
    for name in os.listdir(CLIP_DIR):
        path = os.path.join(CLIP_DIR, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            pass
    return removed


# ---------------------------------------------------------------- helpers

def _binary(name, install_hint):
    """Prefer the Homebrew build: the app runs on an old Python, and the pip
    copies of these tools are pinned to versions that YouTube already broke."""
    for path in (f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}"):
        if os.path.exists(path):
            return path
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"{name} is not installed. Run: {install_hint}")
    return found


def _ffmpeg():
    return _binary("ffmpeg", "brew install ffmpeg")


def _ytdlp():
    return _binary("yt-dlp", "brew install yt-dlp")


def _ytdlp_auth_args():
    """YouTube throttles and bot-checks datacenter IPs far harder than home
    connections, so a hosted deployment usually needs cookies, a proxy, or
    both. Supplied through the environment, never committed."""
    args = []
    cookies = os.environ.get("YTDLP_COOKIES")
    if cookies and os.path.exists(cookies):
        args += ["--cookies", cookies]
    browser = os.environ.get("YTDLP_COOKIES_FROM_BROWSER")
    if browser:
        args += ["--cookies-from-browser", browser]
    proxy = os.environ.get("YTDLP_PROXY")
    if proxy:
        args += ["--proxy", proxy]
    extra = os.environ.get("YTDLP_EXTRA_ARGS")
    if extra:
        args += shlex.split(extra)
    return args


def _run_ytdlp(args, timeout=600):
    proc = subprocess.run([_ytdlp(), "--no-warnings", *_ytdlp_auth_args(), *args],
                          capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout).strip().splitlines()
        msg = [m for m in msg if m.strip()]
        raise RuntimeError(msg[-1] if msg else "yt-dlp failed")
    return proc.stdout


def parse_timestamp(value):
    """Accept 12, 1:51, 1:51.5 or 1:02:03 and return seconds as float."""
    value = str(value).strip()
    if not value:
        raise ValueError("Empty timestamp")
    if not re.fullmatch(r"(\d+:)?(\d+:)?\d+(\.\d+)?", value):
        raise ValueError(f"Invalid timestamp: {value}")
    parts = [float(p) for p in value.split(":")]
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


def parse_range(text):
    """'1:51-2:04' -> (111.0, 124.0)."""
    text = str(text).replace("–", "-").replace("—", "-").strip()
    if "-" not in text:
        raise ValueError("Use the format START-END, for example 1:51-2:04")
    start_raw, end_raw = text.split("-", 1)
    start, end = parse_timestamp(start_raw), parse_timestamp(end_raw)
    if end <= start:
        raise ValueError("End time must be after start time")
    return start, end


def hex_to_rgba(color, alpha=255):
    color = (color or "#ffffff").strip().lstrip("#")
    if len(color) == 3:
        color = "".join(c * 2 for c in color)
    if len(color) != 6:
        color = "ffffff"
    r, g, b = (int(color[i:i + 2], 16) for i in (0, 2, 4))
    return (r, g, b, alpha)


def card_anchor(position, margin_x, margin_y, video_w, video_h, card_w, card_h):
    """Top-left corner of the credit box. Margin is a percentage of the
    video's short side, so the inset looks the same at every resolution and the
    two margins stay directly comparable on non-square formats."""
    position = position if position in POSITIONS else "top-left"
    short = min(video_w, video_h)
    mx = int(short * max(0.0, min(30.0, float(margin_x))) / 100)
    my = int(short * max(0.0, min(30.0, float(margin_y))) / 100)
    vertical, horizontal = position.split("-")
    x = mx if horizontal == "left" else max(0, video_w - card_w - mx)
    y = my if vertical == "top" else max(0, video_h - card_h - my)
    return x, y


def parse_aspect(aspect):
    """Accept '16:9', '16/9' or a plain '1.777' ratio."""
    text = str(aspect or "").strip().replace("/", ":").replace(",", ".")
    try:
        if ":" in text:
            w, h = (float(p) for p in text.split(":", 1))
            ratio = w / h
        else:
            ratio = float(text)
    except (ValueError, ZeroDivisionError):
        raise ValueError(f"Invalid aspect ratio: {aspect}")
    if not 0.1 <= ratio <= 10:
        raise ValueError("Aspect ratio must be between 1:10 and 10:1")
    return ratio


def output_size(resolution, aspect):
    """Resolution is the short side for portrait/square, the height for landscape."""
    ratio = parse_aspect(aspect)
    try:
        base = int(round(float(resolution)))
    except (TypeError, ValueError):
        raise ValueError(f"Invalid resolution: {resolution}")
    if not MIN_RESOLUTION <= base <= MAX_RESOLUTION:
        raise ValueError(
            f"Resolution must be between {MIN_RESOLUTION} and {MAX_RESOLUTION}")
    w_ratio, h_ratio = ratio, 1.0
    if w_ratio >= h_ratio:
        height = base
        width = round(height * w_ratio / h_ratio)
    else:
        width = base
        height = round(width * h_ratio / w_ratio)
    # ffmpeg encoders want even dimensions
    return width - width % 2, height - height % 2


# ---------------------------------------------------------------- metadata

def format_selector(max_height):
    return (f"bestvideo[height<=?{max_height}]+bestaudio/"
            f"best[height<=?{max_height}]/best")


def _streams(info):
    """Direct media URLs (plus the headers they need) for the chosen format.
    Feeding these straight to ffmpeg avoids downloading and re-encoding the
    section as a separate step."""
    picked = info.get("requested_formats") or (
        [info] if info.get("url") else [])
    return [{"url": f["url"], "headers": dict(f.get("http_headers") or {})}
            for f in picked if f.get("url")]


def video_info(url, fmt=None):
    if not re.match(r"https?://", url or ""):
        raise ValueError("Enter a full YouTube link (starting with https://)")
    args = ["--dump-single-json", "--skip-download", "--no-playlist"]
    if fmt:
        args += ["-f", fmt]
    raw = _run_ytdlp(args + [url], timeout=120)
    info = json.loads(raw)

    channel_url = info.get("channel_url") or info.get("uploader_url") or ""
    return {
        "streams": _streams(info) if fmt else [],
        "id": info.get("id"),
        "title": info.get("title") or "",
        "channel": info.get("channel") or info.get("uploader") or "",
        "channel_url": channel_url,
        "duration": info.get("duration") or 0,
        "thumbnail": info.get("thumbnail"),
        "avatar": _avatar_url(info.get("thumbnails"), channel_url),
    }


_AVATAR_CACHE = {}


def _avatar_url(thumbnails, channel_url):
    """The channel's logo. YouTube only exposes it on the channel page for
    most videos, so fall back to that lookup and cache it per channel."""
    for t in thumbnails or []:
        if "avatar" in str(t.get("id", "")):
            return t.get("url")
    if not channel_url:
        return None
    if channel_url in _AVATAR_CACHE:
        return _AVATAR_CACHE[channel_url]

    url = None
    try:
        ch = json.loads(_run_ytdlp([
            "--dump-single-json", "--skip-download", "--flat-playlist",
            "--playlist-items", "0", channel_url], timeout=90))
        avatars = [t for t in (ch.get("thumbnails") or [])
                   if "avatar" in str(t.get("id", ""))]
        if avatars:
            url = avatars[-1].get("url")
    except Exception:
        pass
    _AVATAR_CACHE[channel_url] = url
    return url


def fetch_image(url):
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        return Image.open(io.BytesIO(resp.content)).convert("RGBA")
    except Exception:
        return None


def channel_avatar(info):
    """Square channel logo; falls back to the video thumbnail."""
    return fetch_image(info.get("avatar")) or fetch_image(info.get("thumbnail"))


# ---------------------------------------------------------------- credit card

def _ellipsize(draw, text, font, max_width):
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return (text + "…") if text else ""


def build_credit_card(title, channel, avatar, video_w, video_h,
                      outline_width=2, outline_color="#ffffff",
                      bg_color="#000000", bg_opacity=0.55,
                      text_color="#ffffff", scale=1.0):
    """Return an RGBA image of the pill-shaped credit box."""
    # Box height scales with the video so it looks the same at any resolution.
    box_h = max(48, int(min(video_w, video_h) * 0.11 * scale))
    radius = box_h / 2.0
    pad = box_h * 0.12
    logo_size = int(box_h - 2 * pad)

    title_font = ImageFont.truetype(FONT_BOLD, max(10, int(box_h * 0.28)))
    channel_font = ImageFont.truetype(FONT_REG, max(9, int(box_h * 0.23)))

    text_left = pad + logo_size + pad * 1.1
    # The right end is a half-circle, so the text needs to clear the curve —
    # a flat padding leaves it visually touching the outline.
    pad_right = box_h * 0.42 + outline_width * scale
    max_text_w = int(min(video_w * 0.62, box_h * 9))

    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    title = _ellipsize(probe, title or "", title_font, max_text_w)
    channel = _ellipsize(probe, channel or "", channel_font, max_text_w)

    text_w = max(probe.textlength(title, font=title_font),
                 probe.textlength(channel, font=channel_font))
    box_w = int(text_left + text_w + pad_right)

    ss = 4  # supersample for smooth corners
    card = Image.new("RGBA", (box_w * ss, box_h * ss), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)
    ow = max(0, int(round(outline_width * scale)))
    inset = (ow * ss) / 2.0
    draw.rounded_rectangle(
        [inset, inset, box_w * ss - 1 - inset, box_h * ss - 1 - inset],
        radius=radius * ss - inset,
        fill=hex_to_rgba(bg_color, int(max(0.0, min(1.0, bg_opacity)) * 255)),
        outline=hex_to_rgba(outline_color) if ow else None,
        width=ow * ss,
    )
    card = card.resize((box_w, box_h), Image.LANCZOS)

    if avatar is not None:
        logo = avatar.copy()
        side = min(logo.size)
        logo = logo.crop(((logo.width - side) // 2, (logo.height - side) // 2,
                          (logo.width - side) // 2 + side,
                          (logo.height - side) // 2 + side))
        logo = logo.resize((logo_size * ss, logo_size * ss), Image.LANCZOS)
        mask = Image.new("L", (logo_size * ss, logo_size * ss), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, logo_size * ss - 1, logo_size * ss - 1], fill=255)
        logo.putalpha(mask)
        logo = logo.resize((logo_size, logo_size), Image.LANCZOS)
        card.alpha_composite(logo, (int(pad), int(pad)))
    else:
        ImageDraw.Draw(card).ellipse(
            [int(pad), int(pad), int(pad) + logo_size, int(pad) + logo_size],
            fill=hex_to_rgba(text_color, 45))

    draw = ImageDraw.Draw(card)
    color = hex_to_rgba(text_color)
    gap = box_h * 0.06
    t_h = title_font.getbbox("Ag")[3] - title_font.getbbox("Ag")[1]
    c_h = channel_font.getbbox("Ag")[3] - channel_font.getbbox("Ag")[1]
    block_h = t_h + gap + c_h
    top = (box_h - block_h) / 2
    draw.text((text_left, top - title_font.getbbox("Ag")[1]), title,
              font=title_font, fill=color)
    draw.text((text_left, top + t_h + gap - channel_font.getbbox("Ag")[1]), channel,
              font=channel_font, fill=color)
    return card


# ---------------------------------------------------------------- rendering

def _fit_filter(width, height, mode):
    if mode == "stretch":
        return f"scale={width}:{height},setsar=1"
    if mode == "zoom":
        return (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1")
    return (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1")


def _stream_inputs(streams, seek):
    """ffmpeg input arguments for each remote media stream, seeked to `seek`.
    Seeking before -i makes ffmpeg jump via HTTP range requests instead of
    reading the file from the start."""
    if not streams:
        raise RuntimeError("No playable media URLs for this video")
    inputs = []
    for s in streams:
        args, headers = [], dict(s.get("headers") or {})
        agent = headers.pop("User-Agent", None)
        if agent:
            args += ["-user_agent", agent]
        if headers:
            args += ["-headers",
                     "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
        if str(s["url"]).startswith("http"):
            args += ["-reconnect", "1", "-reconnect_streamed", "1",
                     "-reconnect_delay_max", "5"]
        inputs.append(args + ["-ss", f"{seek:.3f}", "-i", s["url"]])
    return inputs


def _encode(inputs, card_path, overlay, duration, out_path):
    """Run the overlay/encode pass. `inputs` is one entry per media input:
    either a single combined stream or separate video and audio."""
    card_index = len(inputs)
    audio_index = 1 if len(inputs) > 1 else 0
    filters = "[0:v]" + overlay.format(card=card_index)

    flat = [arg for group in inputs for arg in group]
    base = [_ffmpeg(), "-y", *flat, "-i", card_path,
            "-t", f"{duration:.3f}", "-filter_complex", filters, "-map", "[out]"]
    tail = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path]

    proc = subprocess.run(base + ["-map", f"{audio_index}:a?",
                                  "-c:a", "aac", "-b:a", "192k"] + tail,
                          capture_output=True, text=True)
    if proc.returncode == 0:
        return
    # some sections genuinely have no audio stream — retry silent
    silent = subprocess.run(base + ["-an"] + tail, capture_output=True, text=True)
    if silent.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[-800:] or "ffmpeg failed")


def _hhmmss(seconds):
    seconds = max(0.0, float(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def download_section(url, start, end, workdir, max_height=1080):
    """Fetch only the needed section. Returns (path, offset_of_file_start).
    Only used as a fallback — the fast path streams straight into ffmpeg.
    The cut is a stream copy: verified frame-accurate against a re-encoded
    cut, and re-encoding here would just be thrown away by the overlay pass."""
    pad_start = max(0.0, start - 1.0)
    target = os.path.join(workdir, "source.%(ext)s")
    section = f"*{_hhmmss(pad_start)}-{_hhmmss(end + 1.0)}"
    _run_ytdlp([
        "--no-playlist", "-o", target, "-f", format_selector(max_height),
        "--merge-output-format", "mp4",
        "--download-sections", section,
        "--ffmpeg-location", os.path.dirname(_ffmpeg()),
        url])

    files = [f for f in os.listdir(workdir) if f.startswith("source.")]
    if not files:
        raise RuntimeError("Download failed — nothing was written")
    # keyframe-aligned cuts can start slightly before the requested point;
    # ffmpeg trims the rest against this offset.
    return os.path.join(workdir, files[0]), pad_start


def render_clip(url, time_range, *, resolution="1080", aspect="16:9",
                fit="bars", transition=0.6, credit_duration=10.0,
                outline_width=5, outline_color="#ffffff",
                box_color="#000000", box_opacity=0.55, text_color="#ffffff",
                card_scale=0.9, position="top-left",
                margin_x=4.5, margin_y=4.5, progress=None):
    """Cut the clip, burn in the credit card, return the output filename."""
    def say(msg):
        if progress:
            progress(msg)

    start, end = parse_range(time_range)
    duration = end - start
    if duration > MAX_CLIP_SECONDS:
        raise ValueError(
            f"Clips are limited to {int(MAX_CLIP_SECONDS)} seconds "
            f"(you asked for {int(duration)}).")
    width, height = output_size(resolution, aspect)
    fit = fit if fit in FIT_MODES else "bars"
    transition = max(0.0, min(5.0, float(transition)))
    credit_duration = max(0.5, min(duration, float(credit_duration)))

    # Only fetch as many source pixels as the output can actually show.
    # Zoom-to-fill crops, so it needs enough height to cover a wide source;
    # the other modes never scale past the output height.
    needed = max(height, round(width * 9 / 16)) if fit == "zoom" else height
    max_height = min(2160, needed)

    say("Reading video info…")
    info = video_info(url, fmt=format_selector(max_height))

    os.makedirs(CLIP_DIR, exist_ok=True)
    prune_clips()
    workdir = tempfile.mkdtemp(prefix="clip_")
    try:
        say("Building credit card…")
        avatar = channel_avatar(info)
        card = build_credit_card(
            info["title"], info["channel"], avatar, width, height,
            outline_width=outline_width, outline_color=outline_color,
            bg_color=box_color, bg_opacity=box_opacity, text_color=text_color,
            scale=card_scale)
        card_path = os.path.join(workdir, "card.png")
        card.save(card_path)

        card_x, card_y = card_anchor(position, margin_x, margin_y,
                                     width, height, card.width, card.height)
        hold_end = min(duration, transition + credit_duration)
        out_end = hold_end + transition

        if transition > 0:
            # slide in from the left edge, hold, then slide back out
            travel = card_x + card.width  # distance from fully off-screen left
            x_expr = (
                f"if(lt(t,{transition:.3f}),"
                f"-w+{travel}*t/{transition:.3f},"
                f"if(lt(t,{hold_end:.3f}),{card_x},"
                f"{card_x}-{travel}*(t-{hold_end:.3f})/{transition:.3f}))"
            )
        else:
            x_expr = str(card_x)

        overlay = (f"{_fit_filter(width, height, fit)}[v];"
                   f"[{{card}}:v]format=rgba[card];"
                   f"[v][card]overlay=x='{x_expr}':y={card_y}:"
                   f"enable='between(t,0,{out_end:.3f})'[out]")

        name = f"clip_{uuid.uuid4().hex[:10]}.mp4"
        out_path = os.path.join(CLIP_DIR, name)

        say("Rendering…")
        try:
            # Fast path: ffmpeg range-requests straight into the media URLs, so
            # the section is fetched and encoded in one pass instead of being
            # downloaded, re-encoded by yt-dlp, then encoded again here.
            _encode(_stream_inputs(info["streams"], start), card_path,
                    overlay, duration, out_path)
        except RuntimeError as exc:
            say("Direct stream failed, downloading section…")
            source, source_start = download_section(
                url, start, end, workdir, max_height=max_height)
            _encode([["-ss", f"{start - source_start:.3f}", "-i", source]],
                    card_path, overlay, duration, out_path)

        return {
            "file": name,
            "title": info["title"],
            "channel": info["channel"],
            "duration": round(duration, 2),
            "width": width,
            "height": height,
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
