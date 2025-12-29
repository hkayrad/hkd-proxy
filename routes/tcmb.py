import os
import requests
from urllib.parse import urlencode
from flask import Blueprint, Response, request
from dotenv import load_dotenv

from extensions import cache
from auth import require_api_key

load_dotenv()

tcmb_bp = Blueprint('tcmb', __name__)

# Configuration
TCMB_BASE_URL = "https://evds2.tcmb.gov.tr/service/evds/"
TCMB_API_KEY = os.getenv("TCMB_API_KEY")

@tcmb_bp.route("/tcmb", methods=["GET"])
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
