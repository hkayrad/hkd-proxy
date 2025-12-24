import os
from functools import wraps
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request
from flask_caching import Cache

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)
cache = Cache(config={"CACHE_TYPE": "SimpleCache"})
cache.init_app(app)

# Configuration
TCMB_BASE_URL = "https://evds2.tcmb.gov.tr/service/evds/"
TCMB_API_KEY = os.getenv("TCMB_API_KEY")

# Proxy API authentication configuration
# Comma-separated list of valid API keys for accessing this proxy
PROXY_API_KEYS = set(
    key.strip() for key in os.getenv("PROXY_API_KEYS", "").split(",") if key.strip()
)


def require_api_key(f):
    """
    Decorator to require API key authentication for routes.
    Checks for API key in:
    1. X-API-Key header
    2. Authorization header (Bearer token)
    3. api_key query parameter
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        api_key = None

        # Check X-API-Key header
        if "X-API-Key" in request.headers:
            api_key = request.headers.get("X-API-Key")

        # Check Authorization header (Bearer token)
        elif "Authorization" in request.headers:
            auth_header = request.headers.get("Authorization")
            if auth_header and auth_header.startswith("Bearer "):
                api_key = auth_header[7:]  # Remove 'Bearer ' prefix

        # Check query parameter (least secure, but convenient for testing)
        elif "api_key" in request.args:
            api_key = request.args.get("api_key")

        # Validate API key
        if not PROXY_API_KEYS:
            return Response(
                "API authentication not configured on server. Set PROXY_API_KEYS environment variable.",
                status=500,
            )

        if not api_key:
            return jsonify(
                {
                    "error": "Unauthorized",
                    "message": "API key required. Provide via X-API-Key header, Authorization: Bearer <key>, or api_key query parameter.",
                }
            ), 401

        if api_key not in PROXY_API_KEYS:
            return jsonify({"error": "Forbidden", "message": "Invalid API key."}), 403

        return f(*args, **kwargs)

    return decorated_function


@app.route("/tcmb", methods=["GET"])
@require_api_key
@cache.cached(timeout=3600, query_string=True)
def proxy_tcmb():
    """
    Proxy requests to the TCMB EVDS API.
    Expects query parameters compatible with TCMB EVDS.
    Automatically injects the API key if not provided in the request.

    Authentication required via:
    - X-API-Key header
    - Authorization: Bearer <key> header
    - api_key query parameter
    """
    if not TCMB_API_KEY:
        return Response("TCMB_API_KEY not configured on server.", status=500)

    # Prepare parameters (exclude api_key if it was passed as query param for auth)
    params = {k: v for k, v in request.args.to_dict().items() if k != "api_key"}

    upstream_headers = {"Key": TCMB_API_KEY}

    try:
        # Construct the URL manually to match the required format: base + query_string
        # Note: TCMB API seems to append params directly to the base path without '?'
        query_string = urlencode(params)
        target_url = f"{TCMB_BASE_URL}{query_string}"

        # Forward the request to TCMB
        # using stream=True to handle potentially large responses efficiently
        upstream_response = requests.get(
            target_url, headers=upstream_headers, stream=True, timeout=30
        )

        # Filter out Hop-by-hop headers that shouldn't be proxied
        excluded_headers = [
            "content-encoding",
            "content-length",
            "transfer-encoding",
            "connection",
        ]
        headers = [
            (name, value)
            for (name, value) in upstream_response.raw.headers.items()
            if name.lower() not in excluded_headers
        ]

        # Return the response to the client
        return Response(
            upstream_response.content,
            status=upstream_response.status_code,
            headers=headers,
        )

    except requests.RequestException as e:
        return Response(f"Error forwarding request: {str(e)}", status=502)


@app.route("/health", methods=["GET"])
def health_check():
    """
    Health check endpoint (no authentication required).
    """
    return jsonify(
        {
            "status": "healthy",
            "tcmb_api_configured": bool(TCMB_API_KEY),
            "proxy_auth_configured": bool(PROXY_API_KEYS),
        }
    )


if __name__ == "__main__":
    # Listen on all interfaces so it's accessible on the network (e.g., Raspberry Pi)
    app.run(host="0.0.0.0", port=5000)
