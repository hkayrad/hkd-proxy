import os
from flask import Blueprint, jsonify
from dotenv import load_dotenv

load_dotenv()

health_bp = Blueprint('health', __name__)

@health_bp.route("/health", methods=["GET"])
def health_check():
    """
    Health check endpoint (no authentication required).
    """
    tcmb_api_key = os.getenv("TCMB_API_KEY")
    proxy_api_keys = os.getenv("PROXY_API_KEYS")
    
    return jsonify(
        {
            "status": "healthy",
            "tcmb_api_configured": bool(tcmb_api_key),
            "proxy_auth_configured": bool(proxy_api_keys),
        }
    )
