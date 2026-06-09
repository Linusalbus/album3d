"""
Bambu Lab filament color matching using CIE Lab Delta-E distance.
Generates deep-link store URLs using Bambu's base64-encoded ?p= variant parameter.
"""

import math, json, base64

# ── Regional store base URLs ──────────────────────────────────────────────────
STORE_REGIONS = {
    "US": "https://us.store.bambulab.com",
    "EU": "https://eu.store.bambulab.com",
    "UK": "https://uk.store.bambulab.com",
    "AU": "https://au.store.bambulab.com",
    "CA": "https://ca.store.bambulab.com",
}

# ── Product slugs per filament type ───────────────────────────────────────────
TYPE_SLUGS = {
    "PLA Basic":  "/products/pla-basic-filament",
    "PLA Matte":  "/products/pla-matte",
    "PLA Silk":   "/products/pla-silk-upgrade",
    "Support":    "/products/support-for-pla",
}

# ── Color variant strings exactly as Bambu uses them in the ?p= parameter ─────
# Format: "Display Name (XXXXX)"  — None means no deep-link, fallback to product page
# Codes sourced from Bambu community forum, retailer listings, and store URLs.
BAMBU_FILAMENTS = [
    # ── PLA Basic ─────────────────────────────────────────────────────────────
    {"name": "Jade White",       "type": "PLA Basic", "hex": "#FFFFFF", "color_id": "Jade White (10100)"},
    {"name": "Black",            "type": "PLA Basic", "hex": "#1C1C1E", "color_id": "Black (10101)"},
    {"name": "Silver",           "type": "PLA Basic", "hex": "#A6A9AA", "color_id": "Silver (10102)"},
    {"name": "Grey",             "type": "PLA Basic", "hex": "#8E9089", "color_id": "Gray (10103)"},
    {"name": "Dark Grey",        "type": "PLA Basic", "hex": "#4A4A4A", "color_id": "Dark Gray (10105)"},
    {"name": "Red",              "type": "PLA Basic", "hex": "#C12E1F", "color_id": "Red (10200)"},
    {"name": "Magenta",          "type": "PLA Basic", "hex": "#EC008C", "color_id": "Magenta (10202)"},
    {"name": "Hot Pink",         "type": "PLA Basic", "hex": "#F5547C", "color_id": "Hot Pink (10204)"},
    {"name": "Maroon Red",       "type": "PLA Basic", "hex": "#9D2235", "color_id": "Maroon Red (10205)"},
    {"name": "Pumpkin Orange",   "type": "PLA Basic", "hex": "#FF9016", "color_id": "Pumpkin Orange (10301)"},
    {"name": "Sunflower Yellow", "type": "PLA Basic", "hex": "#FEC600", "color_id": "Sunflower Yellow (10401)"},
    {"name": "Bambu Green",      "type": "PLA Basic", "hex": "#00AE42", "color_id": "Bambu Green (10501)"},
    {"name": "Mistletoe Green",  "type": "PLA Basic", "hex": "#3F8E43", "color_id": "Mistletoe Green (10502)"},
    {"name": "Bright Green",     "type": "PLA Basic", "hex": "#BECF00", "color_id": "Bright Green (10503)"},
    {"name": "Blue",             "type": "PLA Basic", "hex": "#0A2989", "color_id": "Blue (10601)"},
    {"name": "Cyan",             "type": "PLA Basic", "hex": "#0086D6", "color_id": "Cyan (10603)"},
    {"name": "Cobalt Blue",      "type": "PLA Basic", "hex": "#0056B8", "color_id": "Cobalt Blue (10604)"},
    {"name": "Turquoise",        "type": "PLA Basic", "hex": "#00B1B7", "color_id": "Turquoise (10605)"},
    {"name": "Purple",           "type": "PLA Basic", "hex": "#5E43B7", "color_id": "Purple (10700)"},
    {"name": "Indigo Purple",    "type": "PLA Basic", "hex": "#482960", "color_id": "Indigo Purple (10701)"},
    {"name": "Cocoa Brown",      "type": "PLA Basic", "hex": "#6F5034", "color_id": "Cocoa Brown (10802)"},
    # ── PLA Matte ─────────────────────────────────────────────────────────────
    {"name": "Ivory White",      "type": "PLA Matte", "hex": "#FFFFFF", "color_id": "Ivory White (20100)"},
    {"name": "Bone White",       "type": "PLA Matte", "hex": "#CBC6B8", "color_id": "Bone White (20101)"},
    {"name": "Charcoal",         "type": "PLA Matte", "hex": "#000000", "color_id": "Charcoal (20102)"},
    {"name": "Ash Grey",         "type": "PLA Matte", "hex": "#9B9EA0", "color_id": "Ash Gray (20103)"},
    {"name": "Desert Tan",       "type": "PLA Matte", "hex": "#E8DBB7", "color_id": "Desert Tan (20104)"},
    {"name": "Latte Brown",      "type": "PLA Matte", "hex": "#D3B7A7", "color_id": "Latte Brown (20105)"},
    {"name": "Caramel",          "type": "PLA Matte", "hex": "#AE835B", "color_id": "Caramel (20106)"},
    {"name": "Dark Brown",       "type": "PLA Matte", "hex": "#7D6556", "color_id": "Dark Brown (20107)"},
    {"name": "Terracotta",       "type": "PLA Matte", "hex": "#B15533", "color_id": "Terracotta (20108)"},
    {"name": "Scarlet Red",      "type": "PLA Matte", "hex": "#DE4343", "color_id": "Scarlet Red (20200)"},
    {"name": "Dark Red",         "type": "PLA Matte", "hex": "#BB3D43", "color_id": "Dark Red (20201)"},
    {"name": "Plum",             "type": "PLA Matte", "hex": "#950051", "color_id": "Plum (20202)"},
    {"name": "Sakura Pink",      "type": "PLA Matte", "hex": "#E8AFCF", "color_id": "Sakura Pink (20203)"},
    {"name": "Mandarin Orange",  "type": "PLA Matte", "hex": "#F99963", "color_id": "Mandarin Orange (20301)"},
    {"name": "Lemon Yellow",     "type": "PLA Matte", "hex": "#F7D959", "color_id": "Lemon Yellow (20401)"},
    {"name": "Apple Green",      "type": "PLA Matte", "hex": "#C2E189", "color_id": "Apple Green (20501)"},
    {"name": "Grass Green",      "type": "PLA Matte", "hex": "#61C680", "color_id": "Grass Green (20502)"},
    {"name": "Dark Green",       "type": "PLA Matte", "hex": "#68724D", "color_id": "Dark Green (20503)"},
    {"name": "Marine Blue",      "type": "PLA Matte", "hex": "#0078BF", "color_id": "Marine Blue (20601)"},
    {"name": "Lilac Purple",     "type": "PLA Matte", "hex": "#AE96D4", "color_id": "Lilac Purple (20701)"},
    # ── PLA Silk ──────────────────────────────────────────────────────────────
    {"name": "Gold",             "type": "PLA Silk",  "hex": "#D4AF37", "color_id": None},
    {"name": "Silver",           "type": "PLA Silk",  "hex": "#AAAAAA", "color_id": None},
    {"name": "Copper",           "type": "PLA Silk",  "hex": "#B87333", "color_id": None},
    {"name": "Rose Gold",        "type": "PLA Silk",  "hex": "#B76E79", "color_id": None},
]


def build_store_url(filament, region="EU"):
    """Build a deep-link store URL with ?p= variant selector if color_id is known."""
    base = STORE_REGIONS.get(region, STORE_REGIONS["EU"])
    slug = TYPE_SLUGS.get(filament["type"], "/collections/bambu-lab-3d-printer-filament")
    product_url = base + slug

    color_id = filament.get("color_id")
    if not color_id:
        return product_url   # fallback: product page, user picks color manually

    param = json.dumps([
        {"propertyKey": "Color",  "propertyValue": color_id},
        {"propertyKey": "Type",   "propertyValue": ""},
        {"propertyKey": "Size",   "propertyValue": "1kg"},
    ], separators=(',', ':'))
    encoded = base64.b64encode(param.encode()).decode()
    return f"{product_url}?p={encoded}"


# ── Color math ────────────────────────────────────────────────────────────────

def hex_to_rgb(hex_str):
    h = hex_str.lstrip('#')
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def rgb_to_lab(r, g, b):
    def linearize(c):
        c /= 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = linearize(r), linearize(g), linearize(b)
    X = r * 0.4124564 + g * 0.3575761 + b * 0.1804375
    Y = r * 0.2126729 + g * 0.7151522 + b * 0.0721750
    Z = r * 0.0193339 + g * 0.1191920 + b * 0.9503041
    X /= 0.95047; Z /= 1.08883

    def f(t):
        return t ** (1/3) if t > 0.008856 else 7.787 * t + 16/116

    return 116 * f(Y) - 16, 500 * (f(X) - f(Y)), 200 * (f(Y) - f(Z))


def delta_e(lab1, lab2):
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(lab1, lab2)))


def boost_saturation(r, g, b, factor=2.0):
    """Boost saturation in HSL space before matching so desaturated
    blues/greens/reds register as clearly coloured rather than grey."""
    r, g, b = r / 255.0, g / 255.0, b / 255.0
    cmax, cmin = max(r, g, b), min(r, g, b)
    delta = cmax - cmin
    l = (cmax + cmin) / 2.0

    if delta < 0.01:          # already grey – nothing to boost
        return int(r * 255), int(g * 255), int(b * 255)

    # Hue
    if cmax == r:
        h = ((g - b) / delta) % 6
    elif cmax == g:
        h = (b - r) / delta + 2
    else:
        h = (r - g) / delta + 4
    h /= 6.0

    # Boost saturation (clamp to 1)
    s = delta / (1 - abs(2 * l - 1))
    s = min(s * factor, 1.0)

    # Back to RGB
    c = (1 - abs(2 * l - 1)) * s
    x = c * (1 - abs((h * 6) % 2 - 1))
    m = l - c / 2
    i = int(h * 6)
    rgb_map = [(c,x,0),(x,c,0),(0,c,x),(0,x,c),(x,0,c),(c,0,x)]
    r2, g2, b2 = rgb_map[i % 6]
    return int((r2 + m) * 255), int((g2 + m) * 255), int((b2 + m) * 255)


# Pre-compute Lab for every filament
for _f in BAMBU_FILAMENTS:
    _f['lab'] = rgb_to_lab(*hex_to_rgb(_f['hex']))


def _score(lab, filament):
    return round(delta_e(lab, filament['lab']), 1)


def _fmt(f, lab):
    return {
        "name":     f["name"],
        "type":     f["type"],
        "hex":      f["hex"],
        "delta_e":  _score(lab, f),
        "color_id": f.get("color_id"),
        "slug":     TYPE_SLUGS.get(f["type"], ""),
    }


def find_closest_filaments(hex_color, top_n=3, owned=None):
    """
    Always match against the full catalog first (best possible Bambu filament).
    If owned is provided, also find the best match within the owned set separately.
    Returns { "catalog": [...top_n], "owned": [...top_n] or None }
    """
    boosted = boost_saturation(*hex_to_rgb(hex_color), factor=2.0)
    lab = rgb_to_lab(*boosted)

    # Step 1: best matches from full catalog
    catalog_matches = sorted(BAMBU_FILAMENTS, key=lambda f: delta_e(lab, f['lab']))
    catalog = [_fmt(f, lab) for f in catalog_matches[:top_n]]

    # Step 2: best matches from owned set (if any)
    owned_matches = None
    if owned:
        owned_set = set(owned)
        pool = [f for f in BAMBU_FILAMENTS if f["name"] in owned_set]
        if pool:
            scored = sorted(pool, key=lambda f: delta_e(lab, f['lab']))
            owned_matches = [_fmt(f, lab) for f in scored[:top_n]]

    return {"catalog": catalog, "owned": owned_matches}


def store_url(filament_type, region="EU"):
    """Simple product-page URL (no variant)."""
    base = STORE_REGIONS.get(region, STORE_REGIONS["EU"])
    slug = TYPE_SLUGS.get(filament_type, "/collections/bambu-lab-3d-printer-filament")
    return base + slug
