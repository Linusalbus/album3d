"""Semi-automatic outreach: find local businesses, grab their logo, render a
QR table-sign mockup with it, and build a review page with a ready-to-send
message per business. Sending stays manual on purpose — automated bulk
messages break Danish marketing law (markedsføringsloven §10) and get
Instagram accounts banned.

Usage:
    python outreach.py find       # businesses near the origins (OpenStreetMap)
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
OVERPASS = "https://overpass-api.de/api/interpreter"


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


def overpass_query(points, radius, categories):
    parts = []
    for lat, lon in points:
        for cat in categories:
            key, value = cat.split("=", 1)
            parts.append(f'nwr["{key}"="{value}"]["name"]'
                         f'(around:{radius},{lat},{lon});')
    return f"[out:json][timeout:90];({''.join(parts)});out center tags;"


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

    query = overpass_query(points, cfg["radius_m"], cfg["categories"])
    r = s.post(OVERPASS, data={"data": query}, timeout=120)
    r.raise_for_status()
    elements = r.json().get("elements", [])

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
    if lead["website"]:
        return lead["website"]
    return ("https://www.google.com/maps/search/?api=1&query="
            + quote_plus(f"{lead['name']} {lead['address']}"))


def render_face(lead, logo, cfg, accent, k=1.5):
    """The flat sign face. Laid out on an 800x1100 grid, drawn at k times that
    so it stays sharp after the perspective warp."""
    def u(v):
        return int(round(v * k))

    W, H = u(800), u(1100)
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
    f1 = ImageFont.truetype(FONT_BOLD, u(62))
    f2 = ImageFont.truetype(FONT_REG, u(36))
    d.text((W / 2, u(880)), headline, font=f1, fill=(29, 29, 31), anchor="mm")
    d.text((W / 2, u(950)), sub, font=f2, fill=(110, 110, 115), anchor="mm")
    d.rounded_rectangle((W / 2 - u(60), u(1010), W / 2 + u(60), u(1018)), u(4),
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


def render_mockup(lead, cfg):
    logo = Image.open(os.path.join(DATA, lead["logo"])).convert("RGBA")
    accent = accent_color(logo)
    face = render_face(lead, logo, cfg, accent)

    S = 2  # draw at 2x and downsample for smooth edges
    W, H = 1600 * S, 1200 * S
    scene = Image.new("RGBA", (W, H))
    top, bottom = (241, 239, 235), (221, 217, 211)
    grad = ImageDraw.Draw(scene)
    for y in range(H):
        t = y / H
        grad.line((0, y, W, y), fill=tuple(int(top[i] + (bottom[i] - top[i]) * t)
                                           for i in range(3)))

    # Sign quad: slight turn to the right so it reads as a 3D object.
    fh = 780 * S
    fw = int(fh * face.width / face.height)
    cx, base_y = W // 2 - 20 * S, 1010 * S
    tl = (cx - fw // 2, base_y - fh)
    tr = (cx + fw // 2, base_y - fh + 34 * S)
    br = (cx + fw // 2, base_y - 10 * S)
    bl = (cx - fw // 2, base_y)
    depth = 22 * S
    base_color = accent if luminance(accent) < 0.6 else shade(accent, 0.75)

    # Soft contact shadow.
    shadow = Image.new("L", (W, H), 0)
    ImageDraw.Draw(shadow).ellipse((bl[0] - 80 * S, base_y - 10 * S,
                                    br[0] + 140 * S, base_y + 90 * S), fill=120)
    shadow = shadow.filter(ImageFilter.GaussianBlur(28 * S))
    scene.paste(Image.new("RGBA", (W, H), (60, 55, 50, 255)), (0, 0), shadow)

    d = ImageDraw.Draw(scene)
    # Base slab in the logo's colour.
    slab_top = base_y - 26 * S
    slab = [(bl[0] - 60 * S, slab_top + 20 * S), (br[0] + 90 * S, slab_top),
            (br[0] + 90 * S, slab_top + 58 * S), (bl[0] - 60 * S, slab_top + 84 * S)]
    d.polygon(slab, fill=base_color)
    d.polygon([slab[0], slab[1], (slab[1][0], slab[1][1] + 10 * S),
               (slab[0][0], slab[0][1] + 10 * S)], fill=shade(base_color, 1.25))

    warped = perspective(face, [tl, tr, br, bl], (W, H))
    # Edge thickness: a darker copy of the face peeking out behind it on the
    # left, since the right side is the one turning away.
    edge = Image.new("RGBA", (W, H), (208, 204, 197, 255))
    edge.putalpha(warped.getchannel("A"))
    for step in range(depth, 0, -2 * S):
        scene.alpha_composite(edge, (-step, -step // 4))
    scene.alpha_composite(warped)

    out = scene.resize((W // S, H // S), Image.LANCZOS).convert("RGB")
    path = os.path.join(MOCKUPS, f"{lead['id']}.jpg")
    out.save(path, quality=90)
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
    opener = cfg["message_openers"][int(re.sub(r"\D", "", lead["id"]) or 0)
                                    % len(cfg["message_openers"])]
    qr_use = cfg["qr_use"].get(lead["category"], cfg["qr_use"]["default"])
    fields = {"name": lead["name"], "my_name": cfg["my_name"],
              "product": cfg["product"]["name"], "qty": cfg["product"]["qty"],
              "price": cfg["product"]["price_dkk"], "qr_use": qr_use}
    return (opener + " " + cfg["message_body"]).format(**fields)


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
    <textarea rows="8">{esc(message_for(l, cfg))}</textarea>
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
