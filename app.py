from flask import Flask, render_template, request, jsonify, send_from_directory
import os, traceback, zipfile, io
from flask import send_file
from album_converter import process_album, get_spotify_token, search_artist_albums, search_artists
from filament_match import find_closest_filaments, BAMBU_FILAMENTS, STORE_REGIONS, TYPE_SLUGS

app = Flask(__name__)
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")


@app.route("/")
def index():
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


@app.route("/download-zip/<safe>")
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
