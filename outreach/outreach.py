"""Semi-automatic outreach: find local businesses, grab their logo, render a
QR table-sign mockup with it, and build a review page with a ready-to-send
message per business. Sending stays manual on purpose — automated bulk
messages break Danish marketing law (markedsføringsloven §10) and get
Instagram accounts banned.

Usage:
    python outreach.py find       # businesses near home (OpenStreetMap)
    python outreach.py logos      # logo + Instagram/e-mail from each website
    python outreach.py mockups    # one mockup PNG per business with a logo
    python outreach.py page       # data/index.html to review and copy messages
    python outreach.py all        # everything above in order
"""

import argparse
import html
import io
import json
import math
import os
import re
import sys
import time
from html.parser import HTMLParser
from urllib.parse import quote_plus, urljoin, urlparse

import qrcode
import requests
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
LOGOS = os.path.join(DATA, "logos")
MOCKUPS = os.path.join(DATA, "mockups")
LEADS = os.path.join(DATA, "leads.json")

USER_AGENT = "outreach-leads/1.0 (personal small-business outreach)"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
# Public Overpass servers, tried in order: the main one often answers 504
# when it is busy.
OVERPASS_SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]


def _first_font(*candidates):
    for path in candidates:
        if os.path.exists(path):
            return path
    raise RuntimeError(
        "No usable font found. Set OUTREACH_FONT_BOLD and "
        "OUTREACH_FONT_REGULAR to font files.")


FONT_BOLD = os.environ.get("OUTREACH_FONT_BOLD") or _first_font(
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)
FONT_REG = os.environ.get("OUTREACH_FONT_REGULAR") or _first_font(
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "C:/Windows/Fonts/arial.ttf",
)


# ---------------------------------------------------------------- storage

def load_config():
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def load_leads():
    if not os.path.exists(LEADS):
        return []
    with open(LEADS, encoding="utf-8") as f:
        return json.load(f)


def save_leads(leads):
    os.makedirs(DATA, exist_ok=True)
    with open(LEADS, "w", encoding="utf-8") as f:
        json.dump(leads, f, ensure_ascii=False, indent=2)


def session():
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


# ---------------------------------------------------------------- find

def geocode(s, origin):
    if "lat" in origin and "lon" in origin:
        return float(origin["lat"]), float(origin["lon"])
    r = s.get(NOMINATIM, params={"q": origin["address"], "format": "json",
                                 "limit": 1}, timeout=20)
    r.raise_for_status()
    hits = r.json()
    if not hits:
        raise RuntimeError(f"Could not geocode {origin['address']!r}. "
                           "Add \"lat\" and \"lon\" to it in config.json.")
    time.sleep(1)  # Nominatim allows one request per second
    return float(hits[0]["lat"]), float(hits[0]["lon"])


def distance_m(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2)
         * math.sin((lon2 - lon1) / 2) ** 2)
    return 6371000 * 2 * math.asin(math.sqrt(h))


def overpass_query(points, radius, categories, require_website):
    """One clause per tag key and origin (values OR'ed in a regex) keeps the
    query light enough that busy public servers don't time out on it."""
    by_key = {}
    for cat in categories:
        key, value = cat.split("=", 1)
        by_key.setdefault(key, []).append(re.escape(value))
    site = '[~"^(contact:)?website$"~"."]' if require_website else ""
    parts = []
    for lat, lon in points:
        for key, values in by_key.items():
            parts.append(f'nwr["{key}"~"^({"|".join(values)})$"]["name"]{site}'
                         f'(around:{radius},{lat},{lon});')
    return f"[out:json][timeout:180];({''.join(parts)});out center tags;"


def run_overpass(s, query):
    last = None
    for attempt in range(2):
        for url in OVERPASS_SERVERS:
            try:
                r = s.post(url, data={"data": query}, timeout=200)
                r.raise_for_status()
                return r.json().get("elements", [])
            except (requests.RequestException, ValueError) as e:
                last = e
                print(f"  {urlparse(url).netloc} failed ({e.__class__.__name__}), "
                      "trying the next server…")
        time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"All Overpass servers failed, try again in a few "
                       f"minutes or lower radius_m in config.json ({last})")


def normalize_url(url):
    if not url:
        return ""
    url = url.strip().split(";")[0].strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


def instagram_handle(value):
    if not value:
        return ""
    value = value.strip().rstrip("/")
    m = re.search(r"instagram\.com/([A-Za-z0-9_.]+)", value)
    handle = m.group(1) if m else value.lstrip("@")
    return handle if re.fullmatch(r"[A-Za-z0-9_.]{2,30}", handle) else ""


def cmd_find(cfg):
    s = session()
    points = []
    for origin in cfg["origins"]:
        points.append(geocode(s, origin))
        print(f"  {origin['label']}: {points[-1][0]:.5f}, {points[-1][1]:.5f}")

    query = overpass_query(points, cfg["radius_m"], cfg["categories"],
                           cfg.get("require_website", True))
    elements = run_overpass(s, query)

    old = {lead["id"]: lead for lead in load_leads()}
    leads = {}
    for el in elements:
        tags = el.get("tags", {})
        lat = el.get("lat") or el.get("center", {}).get("lat")
        lon = el.get("lon") or el.get("center", {}).get("lon")
        if lat is None:
            continue
        lead_id = f"{el['type'][0]}{el['id']}"
        category = next((cat.split("=", 1)[1] for cat in cfg["categories"]
                         if tags.get(cat.split("=", 1)[0]) == cat.split("=", 1)[1]),
                        "default")
        street = " ".join(filter(None, (tags.get("addr:street"),
                                        tags.get("addr:housenumber"))))
        address = ", ".join(filter(None, (street, " ".join(filter(None, (
            tags.get("addr:postcode"), tags.get("addr:city")))))))
        lead = {
            "id": lead_id,
            "name": tags["name"],
            "category": category,
            "address": address,
            "website": normalize_url(tags.get("website")
                                     or tags.get("contact:website")),
            "instagram": instagram_handle(tags.get("contact:instagram")
                                          or tags.get("instagram")),
            "facebook": tags.get("contact:facebook") or tags.get("facebook") or "",
            "email": tags.get("email") or tags.get("contact:email") or "",
            "phone": tags.get("phone") or tags.get("contact:phone") or "",
            "distance_m": round(min(distance_m((lat, lon), p) for p in points)),
            "logo": "",
        }
        # Keep what earlier runs found (logo, scraped Instagram) for this place.
        prev = old.get(lead_id, {})
        for key in ("logo", "instagram", "email"):
            lead[key] = lead[key] or prev.get(key, "")
        leads[lead_id] = lead

    result = sorted(leads.values(), key=lambda l: l["distance_m"])
    if cfg.get("require_website", True):
        result = [l for l in result if l["website"]]
    save_leads(result)
    print(f"Found {len(result)} businesses -> {os.path.relpath(LEADS)}")


# ---------------------------------------------------------------- logos

SKIP_IG = {"p", "reel", "reels", "explore", "stories", "accounts", "tv",
           "sharer", "share", "instagram"}


class _PageScan(HTMLParser):
    """Collects logo candidates plus Instagram and e-mail links from a page."""

    def __init__(self):
        super().__init__()
        self.candidates = []  # (score, url)
        self.instagram = []
        self.emails = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "link":
            rel = a.get("rel", "").lower()
            if "apple-touch-icon" in rel:
                self.candidates.append((3, a.get("href")))
            elif "icon" in rel:
                size = re.findall(r"\d+", a.get("sizes", ""))
                self.candidates.append((2 if size and int(size[0]) >= 128
                                        else 0.5, a.get("href")))
        elif tag == "meta" and a.get("property", a.get("name", "")).lower() in (
                "og:image", "twitter:image"):
            self.candidates.append((1, a.get("content")))
        elif tag == "img":
            hint = " ".join((a.get("src", ""), a.get("alt", ""),
                             a.get("class", ""), a.get("id", ""))).lower()
            src = a.get("src") or a.get("data-src") or ""
            if "logo" in hint and src:
                self.candidates.append((4 if src.lower().endswith(".png") else 3.5,
                                        src))
        elif tag == "a":
            href = a.get("href", "")
            handle = re.search(r"instagram\.com/([A-Za-z0-9_.]+)", href)
            if handle and handle.group(1).lower() not in SKIP_IG:
                self.instagram.append(handle.group(1))
            if href.lower().startswith("mailto:"):
                self.emails.append(href[7:].split("?")[0])


def _decode_image(content):
    try:
        img = Image.open(io.BytesIO(content))
        img.load()
    except Exception:
        try:
            import cairosvg  # optional: lets SVG logos through
            png = cairosvg.svg2png(bytestring=content, output_width=800)
            img = Image.open(io.BytesIO(png))
            img.load()
        except Exception:
            return None
    return img.convert("RGBA")


def trim(img):
    """Crops away transparent or near-white borders around a logo."""
    alpha_box = img.getchannel("A").point(lambda v: 255 if v > 10 else 0).getbbox()
    if alpha_box:
        img = img.crop(alpha_box)
    rgb = Image.new("RGB", img.size, (255, 255, 255))
    rgb.paste(img, mask=img.getchannel("A"))
    ink = rgb.convert("L").point(lambda v: 255 if v < 240 else 0).getbbox()
    return img.crop(ink) if ink else img


def fetch_logo(s, lead):
    r = s.get(lead["website"], timeout=15)
    r.raise_for_status()
    scan = _PageScan()
    scan.feed(r.text)

    if not lead["instagram"] and scan.instagram:
        lead["instagram"] = instagram_handle(scan.instagram[0])
    if not lead["email"] and scan.emails:
        lead["email"] = scan.emails[0]

    tried = set()
    for _, href in sorted(scan.candidates, key=lambda c: -c[0]):
        if not href:
            continue
        url = urljoin(r.url, href)
        if url in tried:
            continue
        tried.add(url)
        try:
            img_r = s.get(url, timeout=15)
            img_r.raise_for_status()
        except requests.RequestException:
            continue
        img = _decode_image(img_r.content)
        if img is None:
            continue
        img = trim(img)
        if min(img.size) < 48:
            continue
        path = os.path.join(LOGOS, f"{lead['id']}.png")
        img.save(path)
        return path
    return ""


def cmd_logos(cfg, limit=None, force=False):
    os.makedirs(LOGOS, exist_ok=True)
    leads = load_leads()
    s = session()
    done = 0
    for lead in leads:
        manual = os.path.join(LOGOS, f"{lead['id']}.png")
        if os.path.exists(manual) and not force:
            lead["logo"] = os.path.relpath(manual, DATA)
            continue
        if not lead["website"] or (limit and done >= limit):
            continue
        done += 1
        try:
            path = fetch_logo(s, lead)
        except requests.RequestException as e:
            print(f"  ✗ {lead['name']}: {e.__class__.__name__}")
            continue
        lead["logo"] = os.path.relpath(path, DATA) if path else ""
        print(f"  {'✓' if path else '–'} {lead['name']}")
        save_leads(leads)  # survive a Ctrl-C halfway through
    save_leads(leads)
    print(f"{sum(1 for l in leads if l['logo'])} of {len(leads)} have a logo")


# ---------------------------------------------------------------- mockups

def accent_color(logo):
    """Most common clearly-coloured pixel in the logo, else a calm slate."""
    small = logo.copy()
    small.thumbnail((80, 80))
    counts = {}
    for r, g, b, a in small.convert("RGBA").getdata():
        if a < 128:
            continue
        mx, mn = max(r, g, b), min(r, g, b)
        if mx > 235 and mn > 225:  # near white
            continue
        key = (r // 24 * 24, g // 24 * 24, b // 24 * 24)
        weight = 1 + 3 * ((mx - mn) / 255)  # prefer saturated over grey
        counts[key] = counts.get(key, 0) + weight
    if not counts:
        return (47, 58, 69)
    return max(counts, key=counts.get)


def luminance(rgb):
    def ch(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def shade(rgb, factor):
    return tuple(max(0, min(255, int(c * factor))) for c in rgb)


def qr_target(lead):
    """Where the QR code points: the business on Google Maps, one tap from
    writing a review. A `review_url` in leads.json (the direct link from
    their Google Business Profile) wins when you have it."""
    if lead.get("review_url"):
        return lead["review_url"]
    # Short URL = fewer QR modules = easier to print cleanly.
    street = lead["address"].split(",")[0]
    return "https://maps.google.com/?q=" + quote_plus(f"{lead['name']} {street}".strip())


def render_face(lead, logo, cfg, accent, k=1.5):
    """The flat sign face. Laid out on an 800x1120 grid (5:7, the 100x140 mm plate), drawn at k times that
    so it stays sharp after the perspective warp."""
    def u(v):
        return int(round(v * k))

    W, H = u(800), u(1120)
    face = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(face)
    d.rounded_rectangle((0, 0, W - 1, H - 1), radius=u(48), fill=(250, 249, 246))

    # Faint layer lines so it reads as a printed part, not a flat card.
    lines = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lines)
    for y in range(0, H, u(5)):
        ld.line((0, y, W, y), fill=(0, 0, 0, 7))
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, W - 1, H - 1), u(48), fill=255)
    lines.putalpha(ImageChops.multiply(lines.getchannel("A"), mask))
    face.alpha_composite(lines)

    box_w, box_h = u(560), u(250)
    lg = logo.copy()
    scale = min(box_w / lg.width, box_h / lg.height)
    lg = lg.resize((max(1, int(lg.width * scale)), max(1, int(lg.height * scale))),
                   Image.LANCZOS)
    face.alpha_composite(lg, ((W - lg.width) // 2, u(60) + (box_h - lg.height) // 2))

    ink = accent if luminance(accent) < 0.18 else (29, 29, 31)
    qr = qrcode.QRCode(border=0, box_size=10,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(qr_target(lead))
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color=ink, back_color=(250, 249, 246)).convert("RGBA")
    qr_img = qr_img.resize((u(440), u(440)), Image.NEAREST)
    face.alpha_composite(qr_img, ((W - u(440)) // 2, u(360)))

    headline, sub = cfg["sign_text"].get(lead["category"],
                                         cfg["sign_text"]["default"])
    size = u(62)
    f1 = ImageFont.truetype(FONT_BOLD, size)
    while d.textlength(headline, font=f1) > W - u(120) and size > u(30):
        size -= 2
        f1 = ImageFont.truetype(FONT_BOLD, size)
    f2 = ImageFont.truetype(FONT_REG, u(36))
    d.text((W / 2, u(866)), headline, font=f1, fill=(29, 29, 31), anchor="mm")
    d.text((W / 2, u(936)), sub, font=f2, fill=(110, 110, 115), anchor="mm")
    d.rounded_rectangle((W / 2 - u(60), u(990), W / 2 + u(60), u(998)), u(4),
                        fill=accent)
    return face


def _solve(a, b):
    """Gaussian elimination for the 8x8 perspective system (no numpy)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        m[col], m[piv] = m[piv], m[col]
        for r in range(n):
            if r != col:
                f = m[r][col] / m[col][col]
                m[r] = [x - f * y for x, y in zip(m[r], m[col])]
    return [m[i][n] / m[i][i] for i in range(n)]


def perspective(img, corners, size):
    """Maps img onto the quad `corners` (tl, tr, br, bl) inside `size`."""
    w, h = img.size
    src = [(0, 0), (w, 0), (w, h), (0, h)]
    a, b = [], []
    for (x, y), (u, v) in zip(corners, src):
        a.append([x, y, 1, 0, 0, 0, -u * x, -u * y]); b.append(u)
        a.append([0, 0, 0, x, y, 1, -v * x, -v * y]); b.append(v)
    return img.transform(size, Image.PERSPECTIVE, _solve(a, b), Image.BICUBIC)


def remove_flat_background(img, tol=28):
    """Logos often come as JPGs on a white or tinted box. If all four corners
    share a colour, flood that colour away from the edges so only the mark
    lands on the sign."""
    if img.getchannel("A").getextrema()[0] < 250:
        return img  # already has transparency
    rgb = img.convert("RGB")
    w, h = rgb.size
    corners = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]
    ref = rgb.getpixel(corners[0])
    if any(max(abs(a - b) for a, b in zip(rgb.getpixel(c), ref)) > tol
           for c in corners[1:]):
        return img
    marker = (1, 254, 3)
    for c in corners:
        ImageDraw.floodfill(rgb, c, marker, thresh=tol)
    alpha = Image.eval(ImageChops.difference(
        rgb, Image.new("RGB", rgb.size, marker)).convert("L"),
        lambda v: 0 if v < 2 else 255)
    out = img.copy()
    out.putalpha(ImageChops.multiply(img.getchannel("A"), alpha))
    return trim(out)


# A tiny 3D scene: rounded plates extruded along their thickness, shaded with
# one directional light, textured on the front, drawn back to front.

def _v_sub(a, b): return (a[0] - b[0], a[1] - b[1], a[2] - b[2])
def _v_dot(a, b): return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
def _v_cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])
def _v_norm(a):
    n = math.sqrt(_v_dot(a, a)) or 1
    return (a[0] / n, a[1] / n, a[2] / n)


class Camera:
    def __init__(self, eye, target, fov_deg, size):
        self.eye = eye
        self.w, self.h = size
        self.f = _v_norm(_v_sub(target, eye))
        self.r = _v_norm(_v_cross(self.f, (0, 1, 0)))
        self.u = _v_cross(self.r, self.f)
        self.focal = (self.h / 2) / math.tan(math.radians(fov_deg) / 2)

    def project(self, p):
        d = _v_sub(p, self.eye)
        z = _v_dot(d, self.f)
        return (self.w / 2 + _v_dot(d, self.r) / z * self.focal,
                self.h / 2 - _v_dot(d, self.u) / z * self.focal, z)


def rounded_outline(w, h, r, seg=10):
    """Counter-clockwise rounded rectangle centred on the origin."""
    pts = []
    for cx, cy, a0 in ((w / 2 - r, -h / 2 + r, -90), (w / 2 - r, h / 2 - r, 0),
                       (-w / 2 + r, h / 2 - r, 90), (-w / 2 + r, -h / 2 + r, 180)):
        for i in range(seg + 1):
            a = math.radians(a0 + 90 * i / seg)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def standing(x, z, yaw_deg, height, lift=0.0):
    """Local plate space (x across, y up, z out of the face) -> upright in the
    world, bottom edge at `lift`, turned `yaw_deg` about the vertical."""
    c, s_ = math.cos(math.radians(yaw_deg)), math.sin(math.radians(yaw_deg))
    def place(p):
        px, py, pz = p
        return (x + c * px + s_ * pz, lift + py + height / 2, z - s_ * px + c * pz)
    return place


def lying(x, z, yaw_deg, thickness, lift=0.0):
    """Local plate space -> flat on the table, face up, top edge pointing away."""
    c, s_ = math.cos(math.radians(yaw_deg)), math.sin(math.radians(yaw_deg))
    def place(p):
        px, py, pz = p
        wx, wz = px, -py
        return (x + c * wx + s_ * wz, lift + pz + thickness / 2, z - s_ * wx + c * wz)
    return place


def clip_below(outline, y_min):
    """Cuts a convex outline at the horizontal line y = y_min (keeps above)."""
    out = []
    for i, a in enumerate(outline):
        b = outline[(i + 1) % len(outline)]
        if a[1] >= y_min:
            out.append(a)
        if (a[1] >= y_min) != (b[1] >= y_min):
            t = (y_min - a[1]) / (b[1] - a[1])
            out.append((a[0] + (b[0] - a[0]) * t, y_min))
    return out


def plate_faces(w, h, t, r, place, color, texture=None, hidden_below=None):
    """Faces of a rounded plate w x h x t mm; `place` maps local -> world.
    `hidden_below` (local y) cuts off the part sunk into a base."""
    outline = rounded_outline(w, h, r)
    if hidden_below is not None:
        outline = clip_below(outline, hidden_below)
    front = [place((px, py, t / 2)) for px, py in outline]
    back = [place((px, py, -t / 2)) for px, py in reversed(outline)]
    rect = [place((-w / 2, h / 2, t / 2)), place((w / 2, h / 2, t / 2)),
            place((w / 2, -h / 2, t / 2)), place((-w / 2, -h / 2, t / 2))]
    faces = [{"pts": front, "color": color, "texture": texture, "rect": rect},
             {"pts": back, "color": color}]
    n = len(outline)
    for i in range(n):
        (ax, ay), (bx, by) = outline[i], outline[(i + 1) % n]
        faces.append({"pts": [place((ax, ay, t / 2)), place((ax, ay, -t / 2)),
                              place((bx, by, -t / 2)), place((bx, by, t / 2))],
                      "color": color})
    return faces


def _normal(pts):
    # Newell's method: robust for the many-sided rounded faces.
    nx = ny = nz = 0.0
    for i, a in enumerate(pts):
        b = pts[(i + 1) % len(pts)]
        nx += (a[1] - b[1]) * (a[2] + b[2])
        ny += (a[2] - b[2]) * (a[0] + b[0])
        nz += (a[0] - b[0]) * (a[1] + b[1])
    return _v_norm((nx, ny, nz))


LIGHT = _v_norm((-0.35, 0.85, 0.9))


def draw_scene(canvas, cam, faces):
    """Draws one object: back faces culled, the rest sorted far to near.
    Call it per object, farthest object first."""
    visible = []
    for f in faces:
        n = _normal(f["pts"])
        centre = tuple(sum(p[i] for p in f["pts"]) / len(f["pts"]) for i in range(3))
        if _v_dot(n, _v_sub(cam.eye, centre)) <= 0:
            continue  # back face
        f["n"] = n
        f["depth"] = _v_dot(_v_sub(centre, cam.eye), cam.f)
        visible.append(f)
    visible.sort(key=lambda f: -f["depth"])

    d = ImageDraw.Draw(canvas)
    for f in visible:
        light = 0.72 + 0.28 * max(0.0, _v_dot(f["n"], LIGHT))
        pts2 = [cam.project(p)[:2] for p in f["pts"]]
        if f.get("texture") is not None:
            tex = f["texture"]
            tint = Image.new("RGBA", tex.size, tuple(int(255 * light) for _ in range(3)) + (255,))
            lit = ImageChops.multiply(tex, tint)
            lit.putalpha(tex.getchannel("A"))
            rect2 = [cam.project(p)[:2] for p in f["rect"]]
            d.polygon(pts2, fill=shade(f["color"], light))
            warped = perspective(lit, rect2, canvas.size)
            mask = Image.new("L", canvas.size, 0)
            ImageDraw.Draw(mask).polygon(pts2, fill=255)  # only the visible face
            warped.putalpha(ImageChops.multiply(warped.getchannel("A"), mask))
            canvas.alpha_composite(warped)
        else:
            d.polygon(pts2, fill=shade(f["color"], light))


def shadow_layer(size, cam, footprints, blur, strength):
    """Soft shadow from ground-plane polygons (lists of world points, y=0)."""
    mask = Image.new("L", size, 0)
    md = ImageDraw.Draw(mask)
    for poly in footprints:
        md.polygon([cam.project(p)[:2] for p in poly], fill=strength)
    return mask.filter(ImageFilter.GaussianBlur(blur))


def cast(p):
    """Where point p lands on the table along the light direction."""
    k = p[1] / LIGHT[1]
    return (p[0] - LIGHT[0] * k, 0.0, p[2] - LIGHT[2] * k)


# Physical sizes in mm. The card is the same part in both variants; the
# standing one just slots into the base.
PLATE = (100.0, 140.0, 4.0, 6.0)   # width, height, thickness, corner radius
BASE = (130.0, 40.0, 14.0)         # width, depth, height


def _mm(v):
    return f"{v:g}"


def dimension(canvas, cam, a, b, offset, label, S):
    """Draws a dimension line between world points a and b, pushed out by
    `offset` (a world vector), with extension lines, end ticks and a label."""
    d = ImageDraw.Draw(canvas)
    col = (88, 88, 94, 255)
    a2 = tuple(a[i] + offset[i] for i in range(3))
    b2 = tuple(b[i] + offset[i] for i in range(3))
    pa, pb = cam.project(a)[:2], cam.project(b)[:2]
    qa, qb = cam.project(a2)[:2], cam.project(b2)[:2]
    lw = max(1, int(1.6 * S))
    for p, q in ((pa, qa), (pb, qb)):
        # Extension line from a little off the part to just past the dim line.
        d.line((p[0] + (q[0] - p[0]) * 0.25, p[1] + (q[1] - p[1]) * 0.25,
                q[0] + (q[0] - p[0]) * 0.15, q[1] + (q[1] - p[1]) * 0.15),
               fill=(88, 88, 94, 140), width=lw)
    d.line((*qa, *qb), fill=col, width=lw)
    dx, dy = qb[0] - qa[0], qb[1] - qa[1]
    n = math.hypot(dx, dy) or 1
    ux, uy = dx / n, dy / n
    t = 7 * S
    for q, sgn in ((qa, 1), (qb, -1)):  # small arrowheads pointing outwards
        d.polygon([q, (q[0] + sgn * ux * t * 2 - uy * t * 0.6, q[1] + sgn * uy * t * 2 + ux * t * 0.6),
                   (q[0] + sgn * ux * t * 2 + uy * t * 0.6, q[1] + sgn * uy * t * 2 - ux * t * 0.6)],
                  fill=col)
    font = ImageFont.truetype(FONT_BOLD, 24 * S)
    mx, my = (qa[0] + qb[0]) / 2, (qa[1] + qb[1]) / 2
    tw = d.textlength(label, font=font)
    pad_x, pad_y = 10 * S, 6 * S
    box = (mx - tw / 2 - pad_x, my - 14 * S - pad_y, mx + tw / 2 + pad_x, my + 14 * S + pad_y)
    d.rounded_rectangle(box, radius=10 * S, fill=(255, 255, 255, 235),
                        outline=(0, 0, 0, 25), width=max(1, S))
    d.text((mx, my), label, font=font, fill=col, anchor="mm")


def render_mockup(lead, cfg):
    logo = Image.open(os.path.join(DATA, lead["logo"])).convert("RGBA")
    logo = remove_flat_background(logo)
    accent = accent_color(logo)
    face = render_face(lead, logo, cfg, accent)

    S = 2  # supersample for clean edges
    W, H = 1600 * S, 1200 * S
    scene = Image.new("RGBA", (W, H))
    top, bottom = (244, 242, 238), (226, 222, 216)
    g = ImageDraw.Draw(scene)
    for y in range(H):
        t = y / H
        g.line((0, y, W, y), fill=tuple(int(top[i] + (bottom[i] - top[i]) * t)
                                         for i in range(3)))

    cam = Camera(eye=(120, 185, 500), target=(8, 52, 12), fov_deg=30, size=(W, H))

    pw, ph, pt, pr = PLATE
    bw, bd, bh = BASE
    plate_col = (236, 233, 227)
    base_col = (40, 40, 44)  # the base is always printed in black PLA

    stand_x, stand_z, stand_yaw = -38.0, -25.0, 14.0
    base_h = bh
    base_place = lying(stand_x, stand_z, stand_yaw, base_h)
    sink = 9.0  # how deep the card sits in the slot
    upright = standing(stand_x, stand_z, stand_yaw, ph, lift=base_h - sink)
    slot = lying(stand_x, stand_z, stand_yaw, 0.01, lift=base_h)
    flat = lying(58.0, 58.0, -12.0, pt)

    # Shadows: tight contact shadow plus a long soft one along the light.
    def ground(place, w, h):
        return [(p[0], 0.0, p[2]) for p in
                (place((x, y, 0)) for x, y in rounded_outline(w, h, 5))]
    base_fp = ground(base_place, bw, bd)
    flat_fp = ground(flat, pw, ph)
    tops = [cast(upright((x, ph / 2, 0))) for x in (-pw / 2, pw / 2)]
    bots = [(p[0], 0.0, p[2]) for p in
            (upright((x, -ph / 2, 0)) for x in (pw / 2, -pw / 2))]
    soft = shadow_layer((W, H), cam, [tops + bots, base_fp, flat_fp], 34 * S, 80)
    contact = shadow_layer((W, H), cam, [base_fp, flat_fp], 5 * S, 130)
    shadow = ImageChops.lighter(soft, contact)
    scene.paste(Image.new("RGBA", (W, H), (70, 64, 58, 255)), (0, 0), shadow)

    # Far to near: base, the slot the sign sits in, the sign, the flat tile.
    draw_scene(scene, cam, plate_faces(bw, bd, bh, 5, base_place, base_col))
    draw_scene(scene, cam, plate_faces(pw + 4, pt + 3, 0.01, 1.2, slot,
                                       (14, 14, 16))[:1])
    draw_scene(scene, cam, plate_faces(pw, ph, pt, pr, upright, plate_col, face,
                                       hidden_below=-ph / 2 + sink))
    draw_scene(scene, cam, plate_faces(pw, ph, pt, pr, flat, plate_col, face))

    if cfg.get("show_dimensions", False):
        fz = pt / 2
        # Standing card: width over the top, height down the left side.
        dimension(scene, cam, upright((-pw / 2, ph / 2, fz)), upright((pw / 2, ph / 2, fz)),
                  (0, 12, 0), f"{_mm(pw)} mm", S)
        left = _v_sub(upright((-1, 0, 0)), upright((0, 0, 0)))
        dimension(scene, cam, upright((-pw / 2, -ph / 2 + sink, fz)), upright((-pw / 2, ph / 2, fz)),
                  tuple(c * 16 for c in left), f"{_mm(ph)} mm", S)
        # Flat card: its full size along the edge nearest the camera.
        near = _v_sub(flat((0, -1, 0)), flat((0, 0, 0)))
        dimension(scene, cam, flat((-pw / 2, -ph / 2, -pt / 2)), flat((pw / 2, -ph / 2, -pt / 2)),
                  tuple(c * 14 for c in near),
                  f"{_mm(pw)} × {_mm(ph)} × {_mm(pt)} mm", S)

    caption = cfg.get("image_caption")
    if caption:
        d = ImageDraw.Draw(scene)
        font = ImageFont.truetype(FONT_BOLD, 30 * S)
        d.text((W - 56 * S, 56 * S), caption, font=font, fill=(60, 60, 66), anchor="ra")

    out = scene.resize((W // S, H // S), Image.LANCZOS).convert("RGB")
    path = os.path.join(MOCKUPS, f"{lead['id']}.jpg")
    out.save(path, quality=92)
    return path


def cmd_mockups(cfg, limit=None):
    os.makedirs(MOCKUPS, exist_ok=True)
    leads = load_leads()
    done = 0
    for lead in leads:
        if not lead.get("logo") or (limit and done >= limit):
            continue
        render_mockup(lead, cfg)
        done += 1
    print(f"Rendered {done} mockups -> {os.path.relpath(MOCKUPS)}")


# ---------------------------------------------------------------- page

def message_for(lead, cfg):
    return cfg["message"].format(name=lead["name"], my_name=cfg["my_name"])


def cmd_page(cfg):
    leads = [l for l in load_leads()
             if l.get("logo") and os.path.exists(os.path.join(MOCKUPS, f"{l['id']}.jpg"))]
    cards = []
    for l in leads:
        esc = html.escape
        maps = ("https://www.google.com/maps/search/?api=1&query="
                + quote_plus(f"{l['name']} {l['address']}"))
        ig = (f"https://www.instagram.com/{esc(l['instagram'])}/" if l["instagram"]
              else "https://www.google.com/search?q="
              + quote_plus(f"{l['name']} København instagram"))
        links = [f'<a href="{ig}" target="_blank" rel="noopener" class="btn primary">'
                 f'{"Open Instagram" if l["instagram"] else "Find Instagram"}</a>']
        if l["website"]:
            links.append(f'<a href="{esc(l["website"])}" target="_blank" rel="noopener" '
                         f'class="btn">Website</a>')
        links.append(f'<a href="{maps}" target="_blank" rel="noopener" class="btn">Map</a>')
        cards.append(f"""
<article class="card" data-id="{esc(l['id'])}" data-ig="{1 if l['instagram'] else 0}">
  <img src="mockups/{esc(l['id'])}.jpg" alt="Mockup for {esc(l['name'])}" loading="lazy">
  <div class="body">
    <div class="head">
      <h2>{esc(l['name'])}</h2>
      <select class="status" aria-label="Status">
        <option value="new">New</option><option value="sent">Sent</option>
        <option value="replied">Replied</option><option value="won">Won</option>
        <option value="no">No</option>
      </select>
    </div>
    <p class="meta">{esc(l['category'])} · {l['distance_m'] / 1000:.1f} km
      {'· @' + esc(l['instagram']) if l['instagram'] else ''}</p>
    <textarea rows="13">{esc(message_for(l, cfg))}</textarea>
    <div class="actions">
      <button class="btn copy">Copy message</button>
      <a class="btn" href="mockups/{esc(l['id'])}.jpg" download="{esc(l['name'])} mockup.jpg">Save image</a>
      {''.join(links)}
    </div>
  </div>
</article>""")

    page = PAGE_TEMPLATE.replace("{{CARDS}}", "".join(cards)).replace(
        "{{COUNT}}", str(len(leads)))
    path = os.path.join(DATA, "index.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"Review page with {len(leads)} businesses -> {os.path.relpath(path)}")


PAGE_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Outreach Leads</title>
<style>
:root{--bg:#f5f5f7;--card:#fff;--text:#1d1d1f;--muted:#6e6e73;--line:#e5e5ea;
--accent:#0071e3;--accent-text:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#000;--card:#1c1c1e;--text:#f5f5f7;
--muted:#98989d;--line:#38383a;--accent:#0a84ff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{position:sticky;top:0;z-index:1;background:color-mix(in srgb,var(--bg) 85%,transparent);
backdrop-filter:blur(12px);border-bottom:1px solid var(--line);padding:14px 16px}
.bar{max-width:1200px;margin:0 auto;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
h1{font-size:20px;margin:0 auto 0 0}
.stats{color:var(--muted);font-size:13px}
main{max-width:1200px;margin:0 auto;padding:16px;display:grid;gap:16px;
grid-template-columns:repeat(auto-fill,minmax(340px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden;
display:flex;flex-direction:column}
.card img{width:100%;aspect-ratio:4/3;object-fit:cover;display:block}
.body{padding:14px;display:flex;flex-direction:column;gap:8px;flex:1}
.head{display:flex;gap:8px;align-items:start;justify-content:space-between}
h2{font-size:16px;margin:0}
.meta{margin:0;color:var(--muted);font-size:13px}
textarea{width:100%;resize:vertical;border:1px solid var(--line);border-radius:10px;
padding:10px;font:inherit;background:var(--bg);color:var(--text)}
.actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:auto}
.btn,select{border:1px solid var(--line);background:var(--card);color:var(--text);
border-radius:8px;padding:6px 10px;font:inherit;font-size:13px;text-decoration:none;cursor:pointer}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-text)}
.card[data-status=sent]{opacity:.6}.card[data-status=no]{opacity:.35}
.card[data-status=won]{border-color:#34c759}
@media (max-width:420px){main{grid-template-columns:1fr}}
</style></head><body>
<header><div class="bar">
  <h1>Outreach Leads</h1>
  <span class="stats" id="stats"></span>
  <select id="filter" aria-label="Filter">
    <option value="all">All ({{COUNT}})</option><option value="new">Not sent yet</option>
    <option value="ig">Has Instagram</option><option value="sent">Sent</option>
    <option value="replied">Replied</option><option value="won">Won</option>
  </select>
  <button class="btn" id="export">Export CSV</button>
</div></header>
<main>{{CARDS}}</main>
<script>
const KEY = "outreach-status";
let store = {};
try { store = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) {}
const save = () => { try { localStorage.setItem(KEY, JSON.stringify(store)); } catch (e) {} };
const cards = [...document.querySelectorAll(".card")];

function refresh() {
  const f = document.getElementById("filter").value;
  const n = {sent: 0, replied: 0, won: 0};
  for (const c of cards) {
    const s = store[c.dataset.id] || "new";
    c.dataset.status = s;
    if (s in n) n[s]++;
    c.hidden = !(f === "all" || (f === "ig" ? c.dataset.ig === "1" : s === f));
  }
  document.getElementById("stats").textContent =
    `${n.sent + n.replied + n.won} sent · ${n.replied + n.won} replied · ${n.won} won`;
}

for (const c of cards) {
  const sel = c.querySelector(".status");
  sel.value = store[c.dataset.id] || "new";
  sel.onchange = () => { store[c.dataset.id] = sel.value; save(); refresh(); };
  c.querySelector(".copy").onclick = async (e) => {
    const ta = c.querySelector("textarea");
    try { await navigator.clipboard.writeText(ta.value); }
    catch (err) { ta.select(); document.execCommand("copy"); }
    e.target.textContent = "Copied ✓";
    setTimeout(() => e.target.textContent = "Copy message", 1500);
  };
}
document.getElementById("filter").onchange = refresh;
document.getElementById("export").onclick = () => {
  const rows = [["name", "status"]].concat(cards.map(c =>
    [c.querySelector("h2").textContent, store[c.dataset.id] || "new"]));
  const csv = rows.map(r => r.map(v => `"${v.replace(/"/g, '""')}"`).join(",")).join("\\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], {type: "text/csv"}));
  a.download = "outreach-status.csv";
  a.click();
};
refresh();
</script></body></html>
"""


# ---------------------------------------------------------------- cli

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["find", "logos", "mockups", "page", "all"])
    p.add_argument("--limit", type=int, help="only process this many businesses")
    p.add_argument("--force", action="store_true",
                   help="re-download logos even if one is already saved")
    args = p.parse_args()
    cfg = load_config()

    if args.command in ("find", "all"):
        cmd_find(cfg)
    if args.command in ("logos", "all"):
        cmd_logos(cfg, args.limit, args.force)
    if args.command in ("mockups", "all"):
        cmd_mockups(cfg, args.limit)
    if args.command in ("page", "all"):
        cmd_page(cfg)


if __name__ == "__main__":
    sys.exit(main())
