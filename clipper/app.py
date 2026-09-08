from flask import Flask, render_template, request, jsonify, send_from_directory, abort
import os, re, threading, traceback, io
import youtube_clipper

app = Flask(__name__)

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


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


def clipper_page(video_id=None):
    if video_id and not VIDEO_ID.match(video_id):
        abort(404)
    return render_template("clipper.html", video_id=video_id or "")


@app.route("/")
@app.route("/clipper")
def index():
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


@app.route("/clipper/ping")
def clipper_ping():
    """Lets the browser extension discover which port the server is on."""
    return jsonify({"ok": True, "app": "youtube-clipper"})


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
    # Rendering is the expensive part; refusing beats thrashing the box.
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


if __name__ == "__main__":
    os.makedirs(youtube_clipper.CLIP_DIR, exist_ok=True)
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug, port=int(os.environ.get("PORT", 5000)))
