import os
from functools import wraps
from flask import request, Response, jsonify
from dotenv import load_dotenv

load_dotenv()

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
