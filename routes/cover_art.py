import os
import re
import uuid
import logging
import mimetypes
import requests
from flask import Blueprint, Response, request, jsonify, stream_with_context, send_file

from extensions import cache, limiter
from auth import require_api_key

cover_art_bp = Blueprint("cover_art", __name__)
logger = logging.getLogger("hkd-proxy.cover_art")

COVER_ART_DIR = os.getenv("COVER_ART_DIR", "data/covers")

def get_version():
    try:
        with open("VERSION", "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        return "unknown"

VERSION = get_version()
USER_AGENT = f"hkd-proxy/{VERSION} ( https://github.com/hkayrad/hkd-proxy )"
VALID_SIZES = {"original", "250", "500", "1200", "full", "default"}

def sanitize_name(name: str) -> str:
    """Sanitize artist or album name for safe use in file paths."""
    cleaned = re.sub(r'[\/\\:*?"<>|\[\]]', '_', name).strip()
    return cleaned or "unknown"

def find_saved_cover(artist: str, album: str, size: str):
    """
    Search COVER_ART_DIR for an existing cover matching artist, album, and size.
    Filename pattern: '{artist} - {album} ({size}) [{mbid}].{ext}'
    Matches case-insensitively.
    Returns (file_path, mbid) if found, else (None, None).
    """
    if not os.path.isdir(COVER_ART_DIR):
        return None, None

    prefix = f"{sanitize_name(artist).lower()} - {sanitize_name(album).lower()} ({size.lower()}) ["
    try:
        with os.scandir(COVER_ART_DIR) as entries:
            for entry in entries:
                if entry.is_file() and entry.name.lower().startswith(prefix):
                    match = re.search(r'\[([^\]]+)\]\.[^.]+$', entry.name)
                    mbid = match.group(1) if match else None
                    return entry.path, mbid
    except OSError as e:
        logger.error("Error reading cover art directory %s: %s", COVER_ART_DIR, e)

    return None, None

def send_saved_cover(file_path: str, mbid: str = None, content_type: str = None):
    """Serve a cover art image from disk with standard proxy headers."""
    if not content_type:
        content_type, _ = mimetypes.guess_type(file_path)
        if not content_type:
            content_type = "image/jpeg"

    resp = send_file(file_path, mimetype=content_type)
    if mbid:
        resp.headers["X-MusicBrainz-MBID"] = mbid
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp

def save_cover_art(artist: str, album: str, size: str, mbid: str, caa_resp):
    """
    Save the Cover Art Archive response stream to disk.
    Filename format: '{artist} - {album} ({size}) [{mbid}].{ext}'
    Writes to a temporary file first, then atomically replaces.
    Returns (saved_file_path, content_type) on success, or (None, None) on error.
    """
    raw_content_type = caa_resp.headers.get("Content-Type", "image/jpeg")
    content_type = raw_content_type.split(";")[0].strip()
    ext_map = {
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/jpeg": ".jpg",
    }
    ext = ext_map.get(content_type, ".jpg")

    safe_artist = sanitize_name(artist)
    safe_album = sanitize_name(album)
    filename = f"{safe_artist} - {safe_album} ({size}) [{mbid}]{ext}"
    final_path = os.path.join(COVER_ART_DIR, filename)

    try:
        os.makedirs(COVER_ART_DIR, exist_ok=True)
        temp_path = os.path.join(COVER_ART_DIR, f".tmp_{uuid.uuid4().hex}")
        with open(temp_path, "wb") as f:
            for chunk in caa_resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        os.replace(temp_path, final_path)
        logger.info("Saved cover art to disk for artist='%s', album='%s' -> %s", artist, album, final_path)
        return final_path, content_type
    except Exception as e:
        logger.error("Failed to save cover art to disk (%s): %s", final_path, e)
        if "temp_path" in locals() and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        return None, None

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
        logger.info("MBID cache hit for artist='%s', album='%s' -> %s (%s)", artist, album, cached.get("mbid"), cached.get("entity_type"))
        return cached.get("mbid"), cached.get("entity_type"), None, None

    headers = {"User-Agent": USER_AGENT}

    # 1. Search release-group
    rg_query = f'artist:({clean_lucene(artist)}) AND releasegroup:({clean_lucene(album)})'
    logger.info("Querying MusicBrainz release-group for artist='%s', album='%s'", artist, album)
    try:
        rg_resp = requests.get(
            "https://musicbrainz.org/ws/2/release-group/",
            headers=headers,
            params={"query": rg_query, "fmt": "json"},
            timeout=10,
        )
    except requests.RequestException as e:
        logger.error("MusicBrainz release-group request failed for artist='%s', album='%s': %s", artist, album, e)
        return None, None, {"error": "Bad Gateway", "message": f"MusicBrainz request failed: {str(e)}"}, 502

    if rg_resp.status_code == 503:
        logger.warning("MusicBrainz rate limit exceeded (503) for artist='%s', album='%s'", artist, album)
        return None, None, {"error": "Service Unavailable", "message": "MusicBrainz rate limit exceeded. Please try again later."}, 503

    if rg_resp.status_code != 200:
        logger.error("MusicBrainz release-group returned status %s for artist='%s', album='%s'", rg_resp.status_code, artist, album)
        return None, None, {"error": "Bad Gateway", "message": f"MusicBrainz returned status {rg_resp.status_code}."}, 502

    rg_data = rg_resp.json().get("release-groups", [])
    if rg_data:
        mbid = rg_data[0]["id"]
        entity_type = "release-group"
        logger.info("Found MusicBrainz release-group MBID: %s for artist='%s', album='%s'", mbid, artist, album)
        cache.set(cache_key, {"mbid": mbid, "entity_type": entity_type}, timeout=604800)
        return mbid, entity_type, None, None

    # 2. Fallback: Search release
    rel_query = f'artist:({clean_lucene(artist)}) AND release:({clean_lucene(album)})'
    logger.info("No release-group found for artist='%s', album='%s'; trying release search", artist, album)
    try:
        rel_resp = requests.get(
            "https://musicbrainz.org/ws/2/release/",
            headers=headers,
            params={"query": rel_query, "fmt": "json"},
            timeout=10,
        )
    except requests.RequestException as e:
        logger.error("MusicBrainz release request failed for artist='%s', album='%s': %s", artist, album, e)
        return None, None, {"error": "Bad Gateway", "message": f"MusicBrainz request failed: {str(e)}"}, 502

    if rel_resp.status_code == 503:
        logger.warning("MusicBrainz rate limit exceeded (503) during release fallback for artist='%s', album='%s'", artist, album)
        return None, None, {"error": "Service Unavailable", "message": "MusicBrainz rate limit exceeded. Please try again later."}, 503

    if rel_resp.status_code != 200:
        logger.error("MusicBrainz release returned status %s for artist='%s', album='%s'", rel_resp.status_code, artist, album)
        return None, None, {"error": "Bad Gateway", "message": f"MusicBrainz returned status {rel_resp.status_code}."}, 502

    rel_data = rel_resp.json().get("releases", [])
    if rel_data:
        mbid = rel_data[0]["id"]
        entity_type = "release"
        logger.info("Found MusicBrainz release MBID: %s for artist='%s', album='%s'", mbid, artist, album)
        cache.set(cache_key, {"mbid": mbid, "entity_type": entity_type}, timeout=604800)
        return mbid, entity_type, None, None

    logger.warning("No MusicBrainz release found matching artist='%s', album='%s'", artist, album)
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

    logger.info("Received cover art request: artist='%s', album='%s', size='%s' (client IP: %s)", artist, album, raw_size, request.remote_addr)

    if not artist or not album:
        logger.warning("Rejected cover art request: missing 'artist' or 'album' (client IP: %s)", request.remote_addr)
        return jsonify({
            "error": "Bad Request",
            "message": "Both 'artist' and 'album' query parameters are required."
        }), 400

    if raw_size not in VALID_SIZES:
        logger.warning("Rejected cover art request: invalid size '%s' (client IP: %s)", raw_size, request.remote_addr)
        return jsonify({
            "error": "Bad Request",
            "message": "Invalid size parameter. Allowed values: 'original', '250', '500', '1200'."
        }), 400

    size = "original" if raw_size in ("original", "full", "default") else raw_size

    # Check local saved cover art on disk first
    saved_path, cached_mbid = find_saved_cover(artist, album, size)
    if saved_path:
        logger.info(
            "Serving cover art from local cache for artist='%s', album='%s', size='%s' (MBID: %s, file: %s)",
            artist, album, size, cached_mbid, os.path.basename(saved_path)
        )
        return send_saved_cover(saved_path, cached_mbid)

    mbid, entity_type, err_resp, status = fetch_mbid(artist, album)
    if err_resp:
        return jsonify(err_resp), status

    headers = {"User-Agent": USER_AGENT}
    suffix = "front" if size == "original" else f"front-{size}"
    caa_url = f"https://coverartarchive.org/{entity_type}/{mbid}/{suffix}"
    logger.info("Fetching cover art from Cover Art Archive: %s", caa_url)

    try:
        caa_resp = requests.get(caa_url, headers=headers, stream=True, allow_redirects=True, timeout=15)
    except requests.RequestException as e:
        logger.error("Cover Art Archive request failed for %s: %s", caa_url, e)
        return jsonify({"error": "Bad Gateway", "message": f"Cover Art Archive request failed: {str(e)}"}), 502

    # If requested thumbnail is 404, fallback to original front cover
    if caa_resp.status_code == 404 and size != "original":
        fallback_url = f"https://coverartarchive.org/{entity_type}/{mbid}/front"
        logger.info("Thumbnail '%s' not found for MBID %s; attempting fallback to original front cover", size, mbid)
        try:
            caa_resp = requests.get(fallback_url, headers=headers, stream=True, allow_redirects=True, timeout=15)
        except requests.RequestException as e:
            logger.error("Cover Art Archive fallback failed for %s: %s", fallback_url, e)

    if caa_resp.status_code == 404:
        logger.warning("Cover art not found in Cover Art Archive for %s MBID %s (artist='%s', album='%s')", entity_type, mbid, artist, album)
        return jsonify({
            "error": "Not Found",
            "message": f"Cover art not found for artist '{artist}' and album '{album}'."
        }), 404

    if caa_resp.status_code != 200:
        logger.error("Cover Art Archive returned status %s for MBID %s", caa_resp.status_code, mbid)
        return jsonify({
            "error": "Bad Gateway",
            "message": f"Cover Art Archive returned status {caa_resp.status_code}."
        }), 502

    saved_path, detected_content_type = save_cover_art(artist, album, size, mbid, caa_resp)
    if saved_path:
        return send_saved_cover(saved_path, mbid, detected_content_type)

    content_type = caa_resp.headers.get("Content-Type", "image/jpeg")
    content_length = caa_resp.headers.get("Content-Length", "unknown")
    logger.info("Streaming cover art response for MBID %s (Content-Type: %s, Length: %s)", mbid, content_type, content_length)

    response_headers = {
        "X-MusicBrainz-MBID": mbid,
        "Cache-Control": "public, max-age=86400",
    }
    if "Content-Length" in caa_resp.headers:
        response_headers["Content-Length"] = caa_resp.headers["Content-Length"]

    return Response(
        stream_with_context(caa_resp.iter_content(chunk_size=8192)),
        content_type=content_type,
        headers=response_headers,
    )
