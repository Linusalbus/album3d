from flask import Flask, render_template, request, jsonify, send_from_directory, abort
import os, re, threading, traceback, zipfile, io
from flask import send_file
from album_converter import process_album, get_spotify_token, search_artist_albums, search_artists
from filament_match import find_closest_filaments, BAMBU_FILAMENTS, STORE_REGIONS, TYPE_SLUGS
import youtube_clipper

app = Flask(__name__)
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")


@app.after_request
def allow_extension(response):
    """The browser extension talks to this server from a chrome-extension://
    origin. Nothing else is granted access."""
    origin = request.headers.get("Origin", "")
    if origin.startswith(("chrome-extension://", "moz-extension://")):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


@app.route("/clipper/ping")
def clipper_ping():
    """Lets the extension discover which port the server ended up on."""
    return jsonify({"ok": True, "app": "youtube-clipper"})


VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
# Comma-separated so a test domain and the real one can both point here.
CLIP_HOSTS = [h.strip().lower() for h in
              os.environ.get("CLIP_HOST", "klipyoutube").split(",") if h.strip()]


def on_clip_domain():
    """True when served from a clip domain, where the whole site is the
    clipper rather than the album tool."""
    host = request.host.split(":")[0].lower()
    return any(h in host for h in CLIP_HOSTS)


def clipper_page(video_id=None):
    if video_id and not VIDEO_ID.match(video_id):
        abort(404)
    return render_template("clipper.html", video_id=video_id or "")


@app.route("/clipper")
def clipper():
    return clipper_page(request.args.get("v"))


# Swap youtube.com for this domain in the address bar and the same path lands
# on the clipper: /watch?v=ID, /shorts/ID, or a bare /ID like youtu.be uses.
@app.route("/watch")
def clipper_watch():
    return clipper_page(request.args.get("v"))


@app.route("/shorts/<vid>")
@app.route("/live/<vid>")
@app.route("/embed/<vid>")
def clipper_short(vid):
    return clipper_page(vid)


@app.route("/<vid>")
def clipper_bare(vid):
    return clipper_page(vid)


@app.route("/clipper/info")
def clipper_info():
    url = (request.args.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Paste a YouTube link first."}), 400
    try:
        return jsonify(youtube_clipper.video_info(url))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


RENDER_SLOTS = threading.Semaphore(int(os.environ.get("MAX_CONCURRENT_RENDERS", 2)))


@app.route("/clipper/render", methods=["POST"])
def clipper_render():
    d = request.get_json() or {}
    # Rendering is the expensive part; queueing beats thrashing the box.
    if not RENDER_SLOTS.acquire(blocking=False):
        return jsonify({"error": "Server is busy rendering. Try again in a moment."}), 429
    try:
        return _do_render(d)
    finally:
        RENDER_SLOTS.release()


def _do_render(d):
    url = (d.get("url") or "").strip()
    time_range = (d.get("range") or "").strip()
    if not url or not time_range:
        return jsonify({"error": "URL and timestamp range are required."}), 400
    try:
        result = youtube_clipper.render_clip(
            url, time_range,
            resolution=d.get("resolution", "1080"),
            aspect=d.get("aspect", "16:9"),
            fit=d.get("fit", "bars"),
            transition=float(d.get("transition", 0.6)),
            credit_duration=float(d.get("credit_duration", 10.0)),
            outline_width=float(d.get("outline_width", 5)),
            outline_color=d.get("outline_color", "#ffffff"),
            box_color=d.get("box_color", "#000000"),
            box_opacity=float(d.get("box_opacity", 0.55)),
            text_color=d.get("text_color", "#ffffff"),
            card_scale=float(d.get("card_scale", 0.9)),
            position=d.get("position", "top-left"),
            margin_x=float(d.get("margin_x", 4.5)),
            margin_y=float(d.get("margin_y", 4.5)),
        )
        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/clipper/preview", methods=["POST"])
def clipper_preview():
    """Render just the credit card as a PNG so settings can be previewed live."""
    import base64
    d = request.get_json() or {}
    try:
        width, height = youtube_clipper.output_size(
            d.get("resolution", "1080"), d.get("aspect", "16:9"))
        avatar = youtube_clipper.fetch_image(d.get("avatar"))
        card = youtube_clipper.build_credit_card(
            d.get("title", ""), d.get("channel", ""), avatar, width, height,
            outline_width=float(d.get("outline_width", 5)),
            outline_color=d.get("outline_color", "#ffffff"),
            bg_color=d.get("box_color", "#000000"),
            bg_opacity=float(d.get("box_opacity", 0.55)),
            text_color=d.get("text_color", "#ffffff"),
            scale=float(d.get("card_scale", 0.9)))
        x, y = youtube_clipper.card_anchor(
            d.get("position", "top-left"),
            float(d.get("margin_x", 4.5)), float(d.get("margin_y", 4.5)),
            width, height, card.width, card.height)
        buf = io.BytesIO()
        card.save(buf, "PNG")
        return jsonify({
            "png": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(),
            "width": card.width, "height": card.height,
            "video_width": width, "video_height": height,
            "x": x, "y": y,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/clips/<path:filename>")
def serve_clip(filename):
    response = send_from_directory(youtube_clipper.CLIP_DIR, filename)
    if request.args.get("download"):
        # The extension's link is cross-origin from youtube.com, where the
        # HTML download attribute is ignored — the header is what forces it.
        response.headers["Content-Disposition"] = (
            'attachment; filename="%s"' % os.path.basename(filename))
    return response


@app.route("/")
def index():
    if on_clip_domain():
        return clipper_page()
    return render_template("index.html")


@app.route("/search-artists")
def search_artists_route():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    try:
        token = get_spotify_token()
        results = search_artists(token, q)
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/artist-albums/<artist_id>")
def artist_albums(artist_id):
    try:
        token = get_spotify_token()
        albums = search_artist_albums(token, artist_id)
        return jsonify(albums)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/generate", methods=["POST"])
def generate():
    data = request.get_json()
    artist    = (data.get("artist") or "").strip()
    album     = (data.get("album") or "").strip()
    colors    = max(2, min(8,   int(data.get("colors")    or 4)))
    size_mm   = max(50, min(400, float(data.get("size_mm")  or 180)))
    total_mm  = max(1,  min(20,  float(data.get("total_mm") or 3.0)))

    if not artist or not album:
        return jsonify({"error": "Artist and album are required."}), 400

    try:
        result = process_album(artist, album, OUTPUT_DIR,
                               num_colors=colors, size_mm=size_mm, total_mm=total_mm)
        owned = data.get("owned_filaments") or []
        use_owned = bool(owned) and data.get("use_owned", False)
        for color in result["colors"]:
            color["filaments"] = find_closest_filaments(
                color["hex"], top_n=3,
                owned=owned if use_owned else None
            )
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Something went wrong: {str(e)}"}), 500


@app.route("/api/filaments")
def api_filaments():
    return jsonify([{
        "name": f["name"],
        "type": f["type"],
        "hex":  f["hex"],
        "slug": TYPE_SLUGS.get(f["type"], "/collections/bambu-lab-3d-printer-filament"),
    } for f in BAMBU_FILAMENTS])


@app.route("/api/regions")
def api_regions():
    return jsonify(STORE_REGIONS)


@app.route("/download-zip/<path:safe>")
def download_zip(safe):
    obj_path = os.path.join(OUTPUT_DIR, f"{safe}.obj")
    mtl_path = os.path.join(OUTPUT_DIR, f"{safe}.mtl")

    if not os.path.exists(obj_path) or not os.path.exists(mtl_path):
        return "Files not found", 404

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(obj_path, f"{safe}.obj")
        zf.write(mtl_path, f"{safe}.mtl")
    buf.seek(0)
    return send_file(buf, mimetype="application/zip",
                     as_attachment=True, download_name=f"{safe}.zip")


@app.route("/output/<path:filename>")
def serve_output(filename):
    return send_from_directory(OUTPUT_DIR, filename)


if __name__ == "__main__":
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug, port=int(os.environ.get("PORT", 5000)))
