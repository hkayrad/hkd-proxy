import os
import requests
import json
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
    ---
    tags:
      - TCMB Proxy
    parameters:
      - name: X-API-Key
        in: header
        type: string
        required: true
        description: API Key for authentication
      - name: series
        in: query
        type: string
        required: false
        description: EVDS Series code
      - name: startDate
        in: query
        type: string
        required: false
        description: Start date (DD-MM-YYYY)
      - name: endDate
        in: query
        type: string
        required: false
        description: End date (DD-MM-YYYY)
      - name: type
        in: query
        type: string
        required: false
        description: Response type (e.g., json)
    responses:
      200:
        description: Successful response from TCMB
      401:
        description: Unauthorized
      500:
        description: Internal Server Error
      502:
        description: Bad Gateway
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
            target_url, headers=upstream_headers, timeout=30
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

        content = upstream_response.content
        
        # Check if response is JSON and process it to handle null values
        is_json = False
        content_type = upstream_response.headers.get("Content-Type", "")
        if "application/json" in content_type.lower():
            try:
                data = upstream_response.json()
                if isinstance(data, dict) and "items" in data:
                    # Forward-fill logic for null values
                    last_known_values = {}
                    processed_items = []
                    
                    for item in data["items"]:
                        new_item = item.copy()
                        for key, value in item.items():
                            if value is not None:
                                last_known_values[key] = value
                            elif key in last_known_values:
                                new_item[key] = last_known_values[key]
                        processed_items.append(new_item)
                    
                    data["items"] = processed_items
                    # Re-serialize to JSON bytes
                    content = json.dumps(data).encode("utf-8")
                    # Update content-length header if present (though we filtered it out for downstream)
                    # For the returned Response object, Flask calculates length automatically if not provided
                    is_json = True
            except ValueError:
                pass # Not valid JSON, pass through original content

        # Return the response to the client
        return Response(
            content,
            status=upstream_response.status_code,
            headers=headers,
        )

    except requests.RequestException as e:
        return Response(f"Error forwarding request: {str(e)}", status=502)
