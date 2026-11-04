import re
import requests
from flask import Blueprint, Response, request, jsonify, stream_with_context

from extensions import cache, limiter
from auth import require_api_key

cover_art_bp = Blueprint("cover_art", __name__)

def get_version():
    try:
        with open("VERSION", "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        return "unknown"

VERSION = get_version()
USER_AGENT = f"hkd-proxy/{VERSION} ( https://github.com/hkayrad/hkd-proxy )"
VALID_SIZES = {"original", "250", "500", "1200", "full", "default"}

def clean_lucene(term: str) -> str:
    """Escape special Lucene syntax characters for MusicBrainz queries."""
    return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r'\\\1', term)

def fetch_mbid(artist: str, album: str):
    """
    Look up MusicBrainz ID for a given artist and album.
    Searches release-group first (the canonical album entity),
    falling back to release if none found.
    Returns (mbid, entity_type, error_response, status_code).
    """
    cache_key = f"mbid:{artist.lower()}:{album.lower()}"
    cached = cache.get(cache_key)
    if cached:
        return cached.get("mbid"), cached.get("entity_type"), None, None

    headers = {"User-Agent": USER_AGENT}

    # 1. Search release-group
    rg_query = f'artist:({clean_lucene(artist)}) AND releasegroup:({clean_lucene(album)})'
    try:
        rg_resp = requests.get(
            "https://musicbrainz.org/ws/2/release-group/",
            headers=headers,
            params={"query": rg_query, "fmt": "json"},
            timeout=10,
        )
    except requests.RequestException as e:
        return None, None, {"error": "Bad Gateway", "message": f"MusicBrainz request failed: {str(e)}"}, 502

    if rg_resp.status_code == 503:
        return None, None, {"error": "Service Unavailable", "message": "MusicBrainz rate limit exceeded. Please try again later."}, 503

    if rg_resp.status_code != 200:
        return None, None, {"error": "Bad Gateway", "message": f"MusicBrainz returned status {rg_resp.status_code}."}, 502

    rg_data = rg_resp.json().get("release-groups", [])
    if rg_data:
        mbid = rg_data[0]["id"]
        entity_type = "release-group"
        cache.set(cache_key, {"mbid": mbid, "entity_type": entity_type}, timeout=604800)
        return mbid, entity_type, None, None

    # 2. Fallback: Search release
    rel_query = f'artist:({clean_lucene(artist)}) AND release:({clean_lucene(album)})'
    try:
        rel_resp = requests.get(
            "https://musicbrainz.org/ws/2/release/",
            headers=headers,
            params={"query": rel_query, "fmt": "json"},
            timeout=10,
        )
    except requests.RequestException as e:
        return None, None, {"error": "Bad Gateway", "message": f"MusicBrainz request failed: {str(e)}"}, 502

    if rel_resp.status_code == 503:
        return None, None, {"error": "Service Unavailable", "message": "MusicBrainz rate limit exceeded. Please try again later."}, 503

    if rel_resp.status_code != 200:
        return None, None, {"error": "Bad Gateway", "message": f"MusicBrainz returned status {rel_resp.status_code}."}, 502

    rel_data = rel_resp.json().get("releases", [])
    if rel_data:
        mbid = rel_data[0]["id"]
        entity_type = "release"
        cache.set(cache_key, {"mbid": mbid, "entity_type": entity_type}, timeout=604800)
        return mbid, entity_type, None, None

    return None, None, {"error": "Not Found", "message": f"No release found matching artist '{artist}' and album '{album}'."}, 404

@cover_art_bp.route("/cover-art", methods=["GET"])
@require_api_key
@limiter.limit("60 per minute")
def get_cover_art():
    """
    Retrieve album cover art from MusicBrainz and Cover Art Archive.
    ---
    tags:
      - Cover Art Proxy
    parameters:
      - name: artist
        in: query
        type: string
        required: true
        description: Name of the artist or band
      - name: album
        in: query
        type: string
        required: true
        description: Title of the album or release
      - name: size
        in: query
        type: string
        required: false
        description: "Image size: 'original' (default), '250', '500', or '1200'"
      - name: api_key
        in: query
        type: string
        required: false
        description: API Key for authentication (can also be passed via X-API-Key or Authorization header)
      - name: X-API-Key
        in: header
        type: string
        required: false
        description: API Key for authentication
    responses:
      200:
        description: Album cover image binary (e.g., image/jpeg or image/png)
        content:
          image/jpeg:
            schema:
              type: string
              format: binary
      400:
        description: Missing required query parameters or invalid size
      401:
        description: Unauthorized - missing API key
      403:
        description: Forbidden - invalid API key
      404:
        description: Album or cover art not found
      502:
        description: Bad Gateway - error communicating with upstream service
      503:
        description: Service Unavailable - upstream rate limit exceeded
    """
    artist = request.args.get("artist", "").strip()
    album = request.args.get("album", "").strip()
    raw_size = request.args.get("size", "original").strip().lower()

    if not artist or not album:
        return jsonify({
            "error": "Bad Request",
            "message": "Both 'artist' and 'album' query parameters are required."
        }), 400

    if raw_size not in VALID_SIZES:
        return jsonify({
            "error": "Bad Request",
            "message": "Invalid size parameter. Allowed values: 'original', '250', '500', '1200'."
        }), 400

    size = "original" if raw_size in ("original", "full", "default") else raw_size

    mbid, entity_type, err_resp, status = fetch_mbid(artist, album)
    if err_resp:
        return jsonify(err_resp), status

    headers = {"User-Agent": USER_AGENT}
    suffix = "front" if size == "original" else f"front-{size}"
    caa_url = f"https://coverartarchive.org/{entity_type}/{mbid}/{suffix}"

    try:
        caa_resp = requests.get(caa_url, headers=headers, stream=True, allow_redirects=True, timeout=15)
    except requests.RequestException as e:
        return jsonify({"error": "Bad Gateway", "message": f"Cover Art Archive request failed: {str(e)}"}), 502

    # If requested thumbnail is 404, fallback to original front cover
    if caa_resp.status_code == 404 and size != "original":
        fallback_url = f"https://coverartarchive.org/{entity_type}/{mbid}/front"
        try:
            caa_resp = requests.get(fallback_url, headers=headers, stream=True, allow_redirects=True, timeout=15)
        except requests.RequestException:
            pass

    if caa_resp.status_code == 404:
        return jsonify({
            "error": "Not Found",
            "message": f"Cover art not found for artist '{artist}' and album '{album}'."
        }), 404

    if caa_resp.status_code != 200:
        return jsonify({
            "error": "Bad Gateway",
            "message": f"Cover Art Archive returned status {caa_resp.status_code}."
        }), 502

    response_headers = {
        "X-MusicBrainz-MBID": mbid,
        "Cache-Control": "public, max-age=86400",
    }
    if "Content-Length" in caa_resp.headers:
        response_headers["Content-Length"] = caa_resp.headers["Content-Length"]

    return Response(
        stream_with_context(caa_resp.iter_content(chunk_size=8192)),
        content_type=caa_resp.headers.get("Content-Type", "image/jpeg"),
        headers=response_headers,
    )
