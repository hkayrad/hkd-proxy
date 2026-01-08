from flask import Flask
from dotenv import load_dotenv

from extensions import cache, limiter
from routes.tcmb import tcmb_bp
from routes.health import health_bp

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)

# Initialize extensions
cache.init_app(app, config={"CACHE_TYPE": "SimpleCache"})
limiter.init_app(app)

# Default Rate Limits
app.config['RATELIMIT_DEFAULT'] = "2000 per day;500 per hour"

# Register Blueprints
app.register_blueprint(tcmb_bp)
app.register_blueprint(health_bp)

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