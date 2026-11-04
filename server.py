import os
import sys
import logging
from flask import Flask
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Configure logging to stdout for Docker log visibility
log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_name, logging.INFO)

logging.basicConfig(
    level=log_level,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("hkd-proxy")

from extensions import cache, limiter
from routes.tcmb import tcmb_bp
from routes.health import health_bp
from routes.cover_art import cover_art_bp

app = Flask(__name__)

# Integrate with Gunicorn logger if running under Gunicorn
gunicorn_logger = logging.getLogger("gunicorn.error")
if gunicorn_logger.handlers:
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)

# Initialize extensions
cache.init_app(app, config={"CACHE_TYPE": "SimpleCache"})
limiter.init_app(app)

# Default Rate Limits
app.config['RATELIMIT_DEFAULT'] = "2000 per day;500 per hour"

# Register Blueprints
app.register_blueprint(tcmb_bp)
app.register_blueprint(health_bp)
app.register_blueprint(cover_art_bp)
logger.info("Registered blueprints: tcmb, health, cover_art (log_level=%s)", log_level_name)

# Start the background cron scheduler on startup
from routes.tcmb import start_cron_scheduler
start_cron_scheduler()


# Initialize Swagger
from flasgger import Swagger
swagger_config = {
    "headers": [],
    "specs": [
        {
            "endpoint": 'apispec',
            "route": '/apispec.json',
            "rule_filter": lambda rule: True,
            "model_filter": lambda tag: True,
        }
    ],
    "static_url_path": "/flasgger_static",
    "swagger_ui": True,
    "specs_route": "/"
}
Swagger(app, config=swagger_config)

if __name__ == "__main__":
    # Listen on all interfaces so it's accessible on the network (e.g., Raspberry Pi)
    app.run(host="0.0.0.0", port=5000)