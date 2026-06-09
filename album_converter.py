"""
Album Cover → OBJ + MTL (4 farver, lag-på-lag)
"""

import os
import requests
import numpy as np
from PIL import Image
from io import BytesIO
from dotenv import load_dotenv

load_dotenv()

SPOTIFY_CLIENT_ID     = os.environ["SPOTIFY_CLIENT_ID"]
SPOTIFY_CLIENT_SECRET = os.environ["SPOTIFY_CLIENT_SECRET"]

SIZE_MM    = 180
RESOLUTION = 150
TOTAL_MM   = 3.0


def get_spotify_token():
    r = requests.post("https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET))
    r.raise_for_status()
    return r.json()["access_token"]


def search_album(token, artist, album):
    r = requests.get("https://api.spotify.com/v1/search",
        headers={"Authorization": f"Bearer {token}"},
        params={"q": f"album:{album} artist:{artist}", "type": "album", "limit": 1})
    r.raise_for_status()
    items = r.json()["albums"]["items"]
    if not items:
        raise ValueError(f"Not found: {artist} – {album}")
    item = items[0]
    cover_url = sorted(item["images"], key=lambda x: x["width"], reverse=True)[0]["url"]
    return cover_url, item["name"], item["artists"][0]["name"]


def search_artists(token, query):
    r = requests.get("https://api.spotify.com/v1/search",
        headers={"Authorization": f"Bearer {token}"},
        params={"q": query, "type": "artist", "limit": 6})
    r.raise_for_status()
    items = r.json()["artists"]["items"]
    return [{"id": a["id"], "name": a["name"],
             "image": a["images"][0]["url"] if a["images"] else None}
            for a in items]


def search_artist_albums(token, artist_id):
    r = requests.get(f"https://api.spotify.com/v1/artists/{artist_id}/albums",
        headers={"Authorization": f"Bearer {token}"})
    if not r.ok:
        raise ValueError(f"Spotify error {r.status_code}: {r.text}")
    r.raise_for_status()
    seen = set()
    albums = []
    for a in r.json()["items"]:
        name = a["name"]
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        cover = sorted(a["images"], key=lambda x: x["width"], reverse=True)[0]["url"] if a["images"] else None
        albums.append({"name": name, "cover": cover, "year": a["release_date"][:4]})
    return albums


def fetch_image(url):
    r = requests.get(url)
    r.raise_for_status()
    return Image.open(BytesIO(r.content)).convert("RGB")


def _rgb_to_lab_np(rgb):
    """Convert Nx3 float32 RGB array → Nx3 Lab."""
    c = rgb / 255.0
    c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    M = np.array([[0.4124564, 0.3575761, 0.1804375],
                  [0.2126729, 0.7151522, 0.0721750],
                  [0.0193339, 0.1191920, 0.9503041]])
    xyz = c @ M.T
    xyz /= np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, xyz ** (1/3), 7.787 * xyz + 16/116)
    L = 116 * f[:, 1] - 16
    a = 500 * (f[:, 0] - f[:, 1])
    b = 200 * (f[:, 1] - f[:, 2])
    return np.stack([L, a, b], axis=1)


def quantize_image(img, n):
    resized = img.resize((RESOLUTION, RESOLUTION), Image.LANCZOS)
    flat = np.array(resized).astype(np.float32).reshape(-1, 3)

    # K-means++ init
    rng = np.random.default_rng(42)
    centers = [flat[rng.integers(len(flat))].copy()]
    for _ in range(n - 1):
        dists = np.min(
            np.sum((flat[:, None] - np.array(centers)[None, :]) ** 2, axis=2),
            axis=1
        )
        probs = dists / dists.sum()
        centers.append(flat[rng.choice(len(flat), p=probs)].copy())
    centers = np.array(centers)

    # K-means iterations
    for _ in range(60):
        dists  = np.sum((flat[:, None] - centers[None, :]) ** 2, axis=2)
        labels = np.argmin(dists, axis=1)
        new_centers = np.array([
            flat[labels == i].mean(axis=0) if (labels == i).any() else centers[i]
            for i in range(n)
        ])
        if np.allclose(centers, new_centers, atol=0.3):
            break
        centers = new_centers

    # ── De-duplicate: if two centers are visually too similar (ΔE < 20),
    #    replace the smaller cluster with the pixel most distant from all centers.
    MIN_DE = 20.0
    centers_lab = _rgb_to_lab_np(centers)
    changed = True
    while changed:
        changed = False
        for i in range(n):
            for j in range(i + 1, n):
                diff = np.sqrt(np.sum((centers_lab[i] - centers_lab[j]) ** 2))
                if diff < MIN_DE:
                    # Drop the smaller cluster
                    counts = [np.sum(labels == k) for k in range(n)]
                    drop = i if counts[i] <= counts[j] else j
                    # Find pixel furthest from all current centers
                    min_dists = np.min(
                        np.sum((flat[:, None] - centers[None, :]) ** 2, axis=2),
                        axis=1
                    )
                    centers[drop] = flat[np.argmax(min_dists)].copy()
                    centers_lab = _rgb_to_lab_np(centers)
                    # Re-run a few iterations to settle
                    for _ in range(20):
                        dists  = np.sum((flat[:, None] - centers[None, :]) ** 2, axis=2)
                        labels = np.argmin(dists, axis=1)
                        new_c  = np.array([
                            flat[labels == k].mean(axis=0) if (labels == k).any() else centers[k]
                            for k in range(n)
                        ])
                        if np.allclose(centers, new_c, atol=0.3):
                            break
                        centers = new_c
                    centers_lab = _rgb_to_lab_np(centers)
                    changed = True
                    break
            if changed:
                break

    centers_u8 = np.clip(centers, 0, 255).astype(np.uint8)
    label_img  = labels.reshape(RESOLUTION, RESOLUTION).astype(np.uint8)

    palette_img = Image.fromarray(label_img, mode='P')
    pal = np.zeros(256 * 3, dtype=np.uint8)
    for i, c in enumerate(centers_u8):
        pal[i * 3:i * 3 + 3] = c
    palette_img.putpalette(pal)
    return palette_img


def rgb_to_hex6(rgb):
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def build_obj(layers_data, pixel_mm, img_height, obj_path, mtl_name):
    lines = []
    lines.append(f"mtllib {mtl_name}")
    lines.append("")

    vert_offset = 1

    for layer_idx, (rgb, mask, z_bot, z_top) in enumerate(layers_data):
        mat_name = f"Lag_{layer_idx+1}"
        rows, cols = np.where(mask)
        if len(rows) == 0:
            continue

        lines.append(f"# {mat_name}  RGB{rgb}  z={z_bot:.2f}->{z_top:.2f}mm")
        lines.append(f"g {mat_name}")
        lines.append(f"usemtl {mat_name}")

        verts = []
        faces = []
        vm = {}
        vc = 0

        def av(x, y, z):
            nonlocal vc
            k = (round(x, 3), round(y, 3), round(z, 3))
            if k not in vm:
                verts.append(f"v {x:.3f} {y:.3f} {z:.3f}")
                vm[k] = vc
                vc += 1
            return vm[k]

        def af(a, b, c):
            faces.append(f"f {a+vert_offset} {b+vert_offset} {c+vert_offset}")

        for r, c in zip(rows, cols):
            x0 = c * pixel_mm
            x1 = x0 + pixel_mm
            y0 = (img_height - r - 1) * pixel_mm
            y1 = y0 + pixel_mm

            b000 = av(x0, y0, z_bot); b100 = av(x1, y0, z_bot)
            b110 = av(x1, y1, z_bot); b010 = av(x0, y1, z_bot)
            t000 = av(x0, y0, z_top); t100 = av(x1, y0, z_top)
            t110 = av(x1, y1, z_top); t010 = av(x0, y1, z_top)

            af(t000, t100, t110); af(t000, t110, t010)
            af(b000, b110, b100); af(b000, b010, b110)
            af(b000, b100, t100); af(b000, t100, t000)
            af(b100, b110, t110); af(b100, t110, t100)
            af(b110, b010, t010); af(b110, t010, t110)
            af(b010, b000, t000); af(b010, t000, t010)

        lines += verts
        lines += faces
        lines.append("")
        vert_offset += vc

    with open(obj_path, 'w') as f:
        f.write('\n'.join(lines))


def build_mtl(layers_data, mtl_path):
    lines = []
    for i, (rgb, _, _, _) in enumerate(layers_data):
        r, g, b = rgb[0] / 255, rgb[1] / 255, rgb[2] / 255
        lines.append(f"newmtl Lag_{i+1}")
        lines.append(f"Kd {r:.4f} {g:.4f} {b:.4f}")
        lines.append(f"Ka 0.0 0.0 0.0")
        lines.append(f"Ks 0.0 0.0 0.0")
        lines.append("")

    with open(mtl_path, 'w') as f:
        f.write('\n'.join(lines))


def process_album(artist, album_name, output_dir, num_colors=4, size_mm=180, total_mm=3.0):
    layer_mm = total_mm / num_colors
    os.makedirs(output_dir, exist_ok=True)
    safe = (f"{artist}_{album_name}_{num_colors}c_{size_mm}mm"
            .replace(" ", "_").replace("/", "-")[:72])

    token = get_spotify_token()
    cover_url, found_album, found_artist = search_album(token, artist, album_name)

    img = fetch_image(cover_url)
    original_path = os.path.join(output_dir, f"{safe}_original.png")
    img.save(original_path)

    quantized = quantize_image(img, num_colors)
    preview_path = os.path.join(output_dir, f"{safe}_preview.png")
    quantized.convert("RGB").save(preview_path)

    arr = np.array(quantized)
    palette = np.array(quantized.getpalette()).reshape(-1, 3)
    pixel_mm = size_mm / RESOLUTION
    H = arr.shape[0]

    # Sort by pixel count descending: most pixels = bottom layer (background),
    # fewest pixels = top layer (fine detail / foreground subject).
    used = sorted(set(np.unique(arr).tolist()),
                  key=lambda i: np.sum(arr == i),
                  reverse=True)

    layers_data = []
    for i, orig_idx in enumerate(used):
        rgb = tuple(int(x) for x in palette[orig_idx])
        z_bot = i * layer_mm
        z_top = (i + 1) * layer_mm
        layer_mask = np.zeros(arr.shape, dtype=bool)
        for j in range(i, len(used)):
            layer_mask |= (arr == used[j])
        layers_data.append((rgb, layer_mask, z_bot, z_top))

    obj_name = f"{safe}.obj"
    mtl_name = f"{safe}.mtl"
    obj_path = os.path.join(output_dir, obj_name)
    mtl_path = os.path.join(output_dir, mtl_name)

    build_mtl(layers_data, mtl_path)
    build_obj(layers_data, pixel_mm, H, obj_path, mtl_name)

    colors = [{"hex": rgb_to_hex6(rgb), "rgb": rgb, "layer": i + 1}
              for i, (rgb, _, _, _) in enumerate(layers_data)]

    return {
        "artist": found_artist,
        "album": found_album,
        "safe": safe,
        "obj_name": obj_name,
        "mtl_name": mtl_name,
        "preview_name": f"{safe}_preview.png",
        "original_name": f"{safe}_original.png",
        "colors": colors,
        "size_mm": size_mm,
        "total_mm": round(total_mm, 2),
        "layer_mm": round(layer_mm, 2),
    }
