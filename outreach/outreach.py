"""Semi-automatic outreach: find local businesses, grab their logo, render a
QR table-sign mockup with it, and build a review page with a ready-to-send
message per business. Sending stays manual on purpose — automated bulk
messages break Danish marketing law (markedsføringsloven §10) and get
Instagram accounts banned.

Usage:
    python outreach.py find       # businesses near home (OpenStreetMap)
    python outreach.py logos      # logo + Instagram/e-mail from each website
    python outreach.py followers  # follower counts (+ profile pic as logo)
    python outreach.py mockups    # photoreal Blender render per business
    python outreach.py page       # data/index.html to review and copy messages
    python outreach.py serve      # open the page (needed to copy images)
    python outreach.py all        # everything above, then serve
"""

import argparse
import html
import importlib.util
import io
import json
import math
import os
import re
import shutil
import subprocess
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


LOCATION_PATH = re.compile(
    r"/(butik|butikker|stores?|locations?|afdeling(er)?|restauranter|"
    r"caf[eé]er|shops?|find-\w+|vores-\w+)(/|$)", re.I)


def _norm(name):
    return re.sub(r"[^a-z0-9æøå]", "", name.lower().replace("&", "og"))


def _domain(url):
    return urlparse(url).netloc.lower().removeprefix("www.") if url else ""


def mark_chains(leads, cfg):
    """Flags places that are part of a chain: tagged with a brand in OSM,
    sharing a name or website with another place, linking to a branch page
    of a bigger site, or matching a known chain in config.json."""
    names = {}
    domains = {}
    for l in leads:
        names[_norm(l["name"])] = names.get(_norm(l["name"]), 0) + 1
        d = _domain(l["website"])
        if d:
            domains[d] = domains.get(d, 0) + 1
    known = [_norm(n) for n in cfg.get("chain_names", [])]
    for l in leads:
        n, d = _norm(l["name"]), _domain(l["website"])
        l["chain"] = bool(
            l.get("chain")
            or names[n] > 1
            or (d and domains[d] > 1)
            or (l["website"] and LOCATION_PATH.search(urlparse(l["website"]).path))
            or any(k and k in n for k in known))


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
            "chain": bool(tags.get("brand") or tags.get("brand:wikidata")),
            "logo": "",
        }
        # Keep what earlier runs found (logo, scraped Instagram) for this place.
        prev = old.get(lead_id, {})
        for key in ("logo", "instagram", "email"):
            lead[key] = lead[key] or prev.get(key, "")
        if "followers" in prev:
            lead["followers"] = prev["followers"]
        leads[lead_id] = lead

    result = sorted(leads.values(), key=lambda l: l["distance_m"])
    mark_chains(result, cfg)
    if cfg.get("require_website", False):
        result = [l for l in result if l["website"]]
    if cfg.get("skip_chains", True):
        chains = [l["name"] for l in result if l["chain"]]
        result = [l for l in result if not l["chain"]]
        if chains:
            print(f"  skipped {len(chains)} chains/big players "
                  f"(e.g. {', '.join(sorted(set(chains))[:5])})")
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


# ---------------------------------------------------------------- followers

IG_APP_ID = "936619743392459"  # the public id instagram.com's own web app sends


FOLLOWERS = re.compile(
    r'([\d][\d.,\s\u00a0]*?)\s*(k|m|t\.?|tus\.?|tusind|mio\.?|mill?\.?|million(?:er)?)?'
    r'\s*(?:followers|følgere)', re.I)


def parse_count(num, suffix=None):
    """'1,234' / '1.234' / '12.5K' / '17,4 t.' / '1,2 mio.' -> int."""
    num = re.sub(r"[\s\u00a0]", "", num)
    if suffix:
        mult = 1_000_000 if suffix.lower().startswith("m") else 1000
        return int(round(float(num.replace(",", ".")) * mult))
    return int(re.sub(r"[.,]", "", num))


def follower_count_from_html(text):
    """Follower count from an Instagram profile page, English or Danish."""
    m = re.search(r'"edge_followed_by":\{"count":(\d+)\}', text)
    if m:
        return int(m.group(1))
    for desc in re.findall(r'<meta[^>]+(?:og:description|name="description")[^>]*>', text):
        content = re.search(r'content="([^"]+)"', desc)
        m = FOLLOWERS.search(html.unescape(content.group(1))) if content else None
        if m:
            return parse_count(m.group(1), m.group(2))
    return None


def instagram_profile(s, handle):
    """Best effort, no login: (follower count, profile picture URL). Tries
    Instagram's web API, then the public profile page. Raises
    PermissionError only when both refuse (rate limited / login wall)."""
    refused = False
    r = s.get("https://i.instagram.com/api/v1/users/web_profile_info/",
              params={"username": handle},
              headers={**BROWSER_HEADERS, "x-ig-app-id": IG_APP_ID}, timeout=15)
    if r.status_code == 200:
        try:
            user = r.json()["data"]["user"]
            return (user["edge_followed_by"]["count"],
                    user.get("profile_pic_url_hd") or user.get("profile_pic_url"))
        except (ValueError, KeyError, TypeError):
            pass
    refused = r.status_code in (401, 403, 429)

    r = s.get(f"https://www.instagram.com/{handle}/", headers=BROWSER_HEADERS,
              timeout=15)
    text = r.text
    count = follower_count_from_html(text)
    pic = re.search(r'<meta property="og:image" content="([^"]+)"', text)
    if count is None and (refused or r.status_code in (401, 403, 429)
                          or "/accounts/login" in r.url):
        raise PermissionError(r.status_code)
    return count, html.unescape(pic.group(1)) if pic else None


def save_profile_picture(s, lead, url):
    """Round-cropped Instagram profile picture, used as the logo for places
    without a website."""
    img = _decode_image(s.get(url, timeout=15).content)
    if img is None:
        return
    side = min(img.size)
    img = img.crop(((img.width - side) // 2, (img.height - side) // 2,
                    (img.width + side) // 2, (img.height + side) // 2))
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).ellipse((0, 0, side - 1, side - 1), fill=255)
    img.putalpha(mask)
    path = os.path.join(LOGOS, f"{lead['id']}.png")
    img.save(path)
    lead["logo"] = os.path.relpath(path, DATA)


def too_big(lead, cfg):
    limit = cfg.get("max_instagram_followers")
    if not limit:
        return False
    if lead.get("followers") is None:
        # Unknown: only skip when asked to, and only if there is an Instagram.
        return bool(cfg.get("skip_unknown_followers") and lead.get("instagram"))
    return lead["followers"] > limit


def cmd_followers(cfg, force=False):
    """Looks up follower counts for leads with an Instagram handle, so big
    accounts (already busy, not the target) can be skipped. Counts that came
    back unknown are retried on every run."""
    os.makedirs(LOGOS, exist_ok=True)
    leads = load_leads()
    s = session()
    checked = refusals = 0
    for lead in leads:
        if not lead.get("instagram"):
            continue
        if lead.get("followers") is not None and not force:
            continue
        try:
            lead["followers"], pic = instagram_profile(s, lead["instagram"])
            if pic and not lead.get("logo"):
                save_profile_picture(s, lead, pic)
            refusals = 0
        except PermissionError as e:
            refusals += 1
            print(f"  @{lead['instagram']}: Instagram refused ({e})")
            if refusals >= 3:
                print("  Instagram is blocking lookups right now - try again in an "
                      "hour. Unchecked businesses are kept unless "
                      "skip_unknown_followers is true.")
                break
            time.sleep(10)
            continue
        except requests.RequestException:
            continue
        checked += 1
        f = lead["followers"]
        flag = " -> skipped" if too_big(lead, cfg) else ""
        print(f"  @{lead['instagram']}: {f if f is not None else '?'} followers{flag}")
        save_leads(leads)
        time.sleep(3)  # stay well under Instagram's rate limit
    save_leads(leads)
    with_ig = [l for l in leads if l.get("instagram")]
    unknown = [l for l in with_ig if l.get("followers") is None]
    skipped = sum(1 for l in leads if too_big(l, cfg))
    print(f"Checked {checked}; {skipped} over {cfg.get('max_instagram_followers')} "
          f"followers skipped; {len(unknown)} of {len(with_ig)} still unknown")


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

    # The slogan is left to the customer; one line of placeholder text shows
    # where theirs goes.
    text = cfg["sign_placeholder"]
    size = u(58)
    f1 = ImageFont.truetype(FONT_BOLD, size)
    while d.textlength(text, font=f1) > W - u(140) and size > u(28):
        size -= 2
        f1 = ImageFont.truetype(FONT_BOLD, size)
    d.text((W / 2, u(890)), text, font=f1, fill=(96, 96, 102), anchor="mm")
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
        light = 0.6 + 0.4 * max(0.0, _v_dot(f["n"], LIGHT))
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
PLATE = (100.0, 140.0, 3.0, 6.0)   # width, height, thickness, corner radius
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


def text_logo(name):
    """The business name set as a clean wordmark, for places with no logo."""
    words, lines = name.split(), []
    font = ImageFont.truetype(FONT_BOLD, 120)
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    for w in words:  # greedy wrap into at most two balanced lines
        if lines and probe.textlength(lines[-1] + " " + w, font=font) < 1150:
            lines[-1] += " " + w
        else:
            lines.append(w)
    if len(lines) > 2:
        mid = len(words) // 2
        lines = [" ".join(words[:mid]), " ".join(words[mid:])]
    width = int(max(probe.textlength(l, font=font) for l in lines)) + 20
    img = Image.new("RGBA", (width, 150 * len(lines)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        d.text((width / 2, 75 + 150 * i), line, font=font, fill=(29, 29, 31),
               anchor="mm")
    return trim(img)


def make_face(lead, cfg):
    if lead.get("logo") and os.path.exists(os.path.join(DATA, lead["logo"])):
        logo = Image.open(os.path.join(DATA, lead["logo"])).convert("RGBA")
        logo = remove_flat_background(logo)
    else:
        logo = text_logo(lead["name"])
    return render_face(lead, logo, cfg, accent_color(logo))


def render_mockup_simple(lead, cfg):
    """Fallback renderer (Pillow only) for when Blender isn't installed."""
    face = make_face(lead, cfg)

    S = 2  # supersample for clean edges
    W, H = 1600 * S, 1200 * S
    scene = Image.new("RGBA", (W, H))
    top, bottom = (200, 192, 181), (168, 159, 147)  # warm taupe: contrast for white and black parts
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

    stand_x, stand_z, stand_yaw = -34.0, -25.0, -24.0  # turned so its edge shows
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
    scene.paste(Image.new("RGBA", (W, H), (46, 40, 34, 255)), (0, 0), shadow)

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

    out = scene.resize((W // S, H // S), Image.LANCZOS).convert("RGB")
    path = os.path.join(MOCKUPS, f"{lead['id']}.jpg")
    out.save(path, quality=92)
    return path


def add_caption(path, cfg):
    """The size note top right and a small 'digital image' disclaimer
    bottom left."""
    caption, disclaimer = cfg.get("image_caption"), cfg.get("image_disclaimer")
    if not caption and not disclaimer:
        return
    img = Image.open(path).convert("RGB")
    d = ImageDraw.Draw(img)
    m = img.width // 28
    if caption:
        font = ImageFont.truetype(FONT_BOLD, max(14, img.width // 53))
        d.text((img.width - m, m), caption, font=font, fill=(44, 42, 40), anchor="ra")
    if disclaimer:
        font = ImageFont.truetype(FONT_REG, max(11, img.width // 80))
        d.text((m, img.height - m), disclaimer, font=font, fill=(70, 66, 62),
               anchor="ld")
    img.save(path, quality=92)


def find_blender():
    """Command prefix that runs render_blender.py, or None. Prefers the
    Blender app (works with any Python), then the pip `bpy` module."""
    script = os.path.join(HERE, "render_blender.py")
    candidates = [os.environ.get("BLENDER"),
                  "/Applications/Blender.app/Contents/MacOS/Blender",
                  shutil.which("blender"),
                  "C:/Program Files/Blender Foundation/Blender 5.0/blender.exe"]
    for exe in candidates:
        if exe and os.path.exists(exe):
            return [exe, "-b", "--factory-startup", "-P", script, "--"]
    if importlib.util.find_spec("bpy"):
        return [sys.executable, script]
    return None


SCENE = os.path.join(DATA, "scene")


def _scene_key(cfg):
    with open(os.path.join(HERE, "render_blender.py"), "rb") as f:
        src = f.read()
    return f"{hash_bytes(src)}-{cfg.get('render_samples', 256)}"


def hash_bytes(b):
    import hashlib
    return hashlib.sha1(b).hexdigest()[:12]


def ensure_scene(cfg, blender, force=False):
    """Renders the white/black scene plates once; reused until the scene
    script or sample count changes."""
    key_path = os.path.join(SCENE, "key.txt")
    key = _scene_key(cfg)
    files = [os.path.join(SCENE, n) for n in ("white.npy", "black.npy", "corners.json")]
    if (not force and all(map(os.path.exists, files)) and os.path.exists(key_path)
            and open(key_path).read() == key):
        return
    os.makedirs(SCENE, exist_ok=True)
    spec = os.path.join(SCENE, "spec.json")
    with open(spec, "w") as f:
        json.dump({"samples": cfg.get("render_samples", 256), "out_dir": SCENE}, f)
    print("  Rendering the scene once in Blender (1-3 min; first time on a Mac "
          "also compiles GPU kernels)…", flush=True)
    with open(os.path.join(DATA, "blender.log"), "w", encoding="utf-8") as log:
        proc = subprocess.Popen(blender + [spec], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in proc.stdout:
            log.write(line)
            if line.startswith("rendered "):
                print(f"    {line.strip()} pass", flush=True)
        proc.wait()
    if proc.returncode != 0 or not all(map(os.path.exists, files)):
        sys.exit("Blender failed - see data/blender.log")
    with open(key_path, "w") as f:
        f.write(key)


def _to_linear(u8):
    c = u8.astype("float32") / 255
    import numpy as np
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


# AgX with the "Punchy" look - the same view transform the Blender renders
# used, via the widely used polynomial fit of AgX (matrices are columns).
_AGX_IN = [[0.842479062253094, 0.0423282422610123, 0.0423756549057051],
           [0.0784335999999992, 0.878468636469772, 0.0784336],
           [0.0792237451477643, 0.0791661274605434, 0.879142973793104]]
_AGX_OUT = [[1.19687900512017, -0.0528968517574562, -0.0529716355144438],
            [-0.0980208811401368, 1.15190312990417, -0.0980434501171241],
            [-0.0990297440797205, -0.0989611768448433, 1.15107367264843]]


def _to_display(lin, exposure):
    """Scene-linear Rec.709 -> 8-bit display sRGB through AgX Punchy."""
    import numpy as np
    x = np.maximum(lin * (2 ** exposure), 1e-10) @ np.array(_AGX_IN, dtype="float32")
    lo, hi = -12.47393, 4.026069
    x = (np.clip(np.log2(np.maximum(x, 1e-10)), lo, hi) - lo) / (hi - lo)
    x2 = x * x
    x4 = x2 * x2
    x = (15.5 * x4 * x2 - 40.14 * x4 * x + 31.96 * x4 - 6.868 * x2 * x
         + 0.4298 * x2 + 0.1191 * x - 0.00232)
    luma = x @ np.array([0.2126, 0.7152, 0.0722], dtype="float32")
    x = np.maximum(x, 0) ** 1.35                     # Punchy: power 1.35 ...
    x = luma[..., None] + 1.4 * (x - luma[..., None])  # ... saturation 1.4
    x = x @ np.array(_AGX_OUT, dtype="float32")
    return (np.clip(x, 0, 1) * 255 + 0.5).astype("uint8")


def composite_mockup(lead, cfg, plates, out_path):
    import numpy as np
    white, black, corners = plates
    h, w = white.shape[:2]
    face = make_face(lead, cfg)
    plate_rgb = np.array((238, 236, 231), dtype="float32")
    albedo = np.ones((h, w, 3), dtype="float32")
    for quad in corners.values():
        # Pre-shrink to about twice the on-screen size: avoids QR moire.
        on_screen = max(math.dist(quad[0], quad[3]), math.dist(quad[1], quad[2]))
        scale = min(1.0, 2 * on_screen / face.height)
        small = face.resize((max(1, int(face.width * scale)),
                             max(1, int(face.height * scale))), Image.LANCZOS)
        warped = np.asarray(perspective(small, quad, (w, h)), dtype="float32")
        alpha = warped[..., 3:4] / 255
        rgb = warped[..., :3] * alpha + plate_rgb * (1 - alpha)
        inside = perspective(Image.new("L", small.size, 255), quad, (w, h))
        m = (np.asarray(inside, dtype="float32") / 255)[..., None]
        albedo = albedo * (1 - m) + _to_linear(rgb) * m
    lin = black + albedo * (white - black)
    Image.fromarray(_to_display(lin, cfg.get("exposure", 0.0))).save(out_path, quality=92)
    add_caption(out_path, cfg)


def cmd_mockups(cfg, limit=None, force=False):
    os.makedirs(MOCKUPS, exist_ok=True)
    leads = load_leads()
    mark_chains(leads, cfg)
    leads = [l for l in leads if not too_big(l, cfg) and not l.get("chain")]
    if not force:
        have = [l for l in leads if os.path.exists(os.path.join(MOCKUPS, f"{l['id']}.jpg"))]
        if have:
            print(f"  {len(have)} already rendered - skipping them (--force re-renders all)")
        leads = [l for l in leads if l not in have]
    if limit:
        leads = leads[:limit]
    if not leads:
        print("Nothing to render")
        return

    blender = find_blender() if cfg.get("renderer", "auto") != "simple" else None
    if not blender:
        if cfg.get("renderer") == "blender":
            sys.exit("Blender not found. Install it from blender.org, or set BLENDER "
                     "to its path.")
        print("  Blender not found - using the simple renderer. Install Blender "
              "(blender.org) for photoreal mockups.")
        for lead in leads:
            add_caption(render_mockup_simple(lead, cfg), cfg)
        print(f"Rendered {len(leads)} mockups -> {os.path.relpath(MOCKUPS)}")
        return

    import numpy as np
    ensure_scene(cfg, blender, force=False)
    plates = (np.load(os.path.join(SCENE, "white.npy")).astype("float32"),
              np.load(os.path.join(SCENE, "black.npy")).astype("float32"),
              json.load(open(os.path.join(SCENE, "corners.json"))))
    start = time.time()
    for i, lead in enumerate(leads, 1):
        composite_mockup(lead, cfg, plates, os.path.join(MOCKUPS, f"{lead['id']}.jpg"))
        if i % 10 == 0 or i == len(leads):
            print(f"  [{i}/{len(leads)}] {time.time() - start:.0f}s", flush=True)
    print(f"Rendered {len(leads)} mockups -> {os.path.relpath(MOCKUPS)}")


# ---------------------------------------------------------------- page

def message_for(lead, cfg):
    return cfg["message"].format(name=lead["name"], my_name=cfg["my_name"])


def cmd_page(cfg, quiet=False):
    all_leads = load_leads()
    mark_chains(all_leads, cfg)
    leads = [l for l in all_leads
             if not too_big(l, cfg) and not l.get("chain")
             and os.path.exists(os.path.join(MOCKUPS, f"{l['id']}.jpg"))]
    cards = []
    for l in leads:
        esc = html.escape
        maps = ("https://www.google.com/maps/search/?api=1&query="
                + quote_plus(f"{l['name']} {l['address']}"))
        ig = (f"https://www.instagram.com/{esc(l['instagram'])}/" if l["instagram"]
              else "https://www.google.com/search?q="
              + quote_plus(f"{l['name']} København instagram"))
        links = [f'<a href="{ig}" target="_blank" rel="noopener" class="btn">'
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
      {'· @' + esc(l['instagram']) if l['instagram'] else ''}
      {f"· {l['followers']} followers" if l.get('followers') is not None
       else ('· followers unknown' if l['instagram'] else '')}</p>
    <textarea rows="13">{esc(message_for(l, cfg))}</textarea>
    <div class="actions">
      <button class="btn primary copy">Copy message + image</button>
      <button class="btn copy-img">Copy image</button>
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
    if not quiet:
        hidden = sum(1 for l in all_leads if too_big(l, cfg) or l.get("chain"))
        print(f"Review page with {len(leads)} businesses ({hidden} hidden as chains "
              f"or over the follower limit) -> {os.path.relpath(path)}")


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
.note{max-width:1200px;margin:8px auto 0;padding:0 0;color:var(--muted);font-size:13px}
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
  c.querySelector(".copy").onclick = (e) => copyCard(c, e.target, true);
  c.querySelector(".copy-img").onclick = (e) => copyCard(c, e.target, false);
}

// Clipboard images must be PNG and need the page served over http
// (python outreach.py serve); from file:// only the text can be copied.
async function pngBlob(src) {
  const bmp = await createImageBitmap(await (await fetch(src)).blob());
  const cv = document.createElement("canvas");
  cv.width = bmp.width; cv.height = bmp.height;
  cv.getContext("2d").drawImage(bmp, 0, 0);
  return new Promise(res => cv.toBlob(res, "image/png"));
}

async function copyCard(c, btn, withText) {
  const label = btn.textContent;
  const text = c.querySelector("textarea").value;
  const src = c.querySelector("img").getAttribute("src");
  try {
    const item = {"image/png": pngBlob(src)};
    if (withText) {
      item["text/plain"] = new Blob([text], {type: "text/plain"});
      const png = await item["image/png"];
      const dataUrl = await new Promise(r => { const f = new FileReader();
        f.onload = () => r(f.result); f.readAsDataURL(png); });
      const htmlText = text.split("\\n").map(l => l ? l.replace(/&/g, "&amp;")
        .replace(/</g, "&lt;") : "<br>").join("<br>");
      item["text/html"] = new Blob([`<div>${htmlText}</div><br><img src="${dataUrl}" width="600">`],
        {type: "text/html"});
      item["image/png"] = png;
    }
    await navigator.clipboard.write([new ClipboardItem(item)]);
    btn.textContent = "Copied ✓";
  } catch (err) {
    if (withText) {
      const ta = c.querySelector("textarea");
      try { await navigator.clipboard.writeText(text); }
      catch (e2) { ta.select(); document.execCommand("copy"); }
      btn.textContent = "Text only ✓ (run serve for image)";
    } else {
      btn.textContent = "Run serve to copy images";
    }
  }
  setTimeout(() => btn.textContent = label, 2500);
}

if (location.protocol === "file:") {
  const note = document.createElement("p");
  note.className = "note";
  note.textContent = "Opened as a file, so only text can be copied. Run "
    + '"python outreach.py serve" to copy the image too.';
  document.querySelector("header .bar").after(note);
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


def cmd_serve(port=8765):
    """Serves the review page on localhost (browsers only allow copying
    images from http). The page is rebuilt on every load, so a refresh
    always reflects the latest followers/chain filtering."""
    import functools
    import http.server
    import webbrowser

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] in ("/", "/index.html"):
                cmd_page(load_config(), quiet=True)
                if self.path.startswith("/?") or self.path == "/":
                    self.path = "/index.html"
            super().do_GET()

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def log_message(self, *args):
            pass

    handler = functools.partial(Handler, directory=DATA)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{port}/index.html"
    print(f"Review page: {url}  (refresh to pick up changes, Ctrl+C to stop)")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------- cli

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command",
                   choices=["find", "logos", "followers", "mockups", "page", "all",
                            "serve"])
    p.add_argument("--limit", type=int, help="only process this many businesses")
    p.add_argument("--force", action="store_true",
                   help="redo work already done (logos, follower counts, mockups)")
    args = p.parse_args()
    cfg = load_config()

    if args.command in ("find", "all"):
        cmd_find(cfg)
    if args.command in ("logos", "all"):
        cmd_logos(cfg, args.limit, args.force)
    if args.command in ("followers", "all"):
        cmd_followers(cfg, args.force)
    if args.command in ("mockups", "all"):
        cmd_mockups(cfg, args.limit, args.force)
    if args.command in ("page", "all"):
        cmd_page(cfg)
    if args.command in ("serve", "all"):
        cmd_serve()


if __name__ == "__main__":
    sys.exit(main())
