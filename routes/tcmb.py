import os
import requests
import json
import threading
from datetime import datetime, timedelta
from urllib.parse import urlencode
from flask import Blueprint, Response, request, jsonify
from dotenv import load_dotenv

from extensions import cache, limiter
from auth import require_api_key

load_dotenv()

tcmb_bp = Blueprint('tcmb', __name__)

# Configuration
TCMB_BASE_URL = "https://evds3.tcmb.gov.tr/igmevdsms-dis/"
TCMB_API_KEY = os.getenv("TCMB_API_KEY")
APPRISE_API_URL = os.getenv("APPRISE_API_URL")
APPRISE_NOTIFICATION_URL = os.getenv("APPRISE_NOTIFICATION_URL")

@tcmb_bp.route("/tcmb", methods=["GET"])
@require_api_key
@limiter.limit("60 per minute")
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
                                # Round numeric series values to 4 decimal places
                                if key.startswith("TP"):
                                    try:
                                        value = round(float(value), 4)
                                        new_item[key] = value
                                    except (ValueError, TypeError):
                                        pass
                                last_known_values[key] = value
                            elif key in last_known_values:
                                new_item[key] = last_known_values[key]
                        processed_items.append(new_item)

                    data["items"] = processed_items

                    # Trigger background notification if today's rates are fetched for the first time
                    if processed_items:
                        latest_item = processed_items[-1]
                        rate_date = latest_item.get("Tarih")
                        today_str = datetime.now().strftime("%d-%m-%Y")
                        if rate_date == today_str:
                            trigger_automated_notification_async()

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

@tcmb_bp.route("/tcmb/notify", methods=["POST", "GET"])
@require_api_key
@limiter.limit("10 per minute")
def notify_tcmb_rates():
    """
    Fetch today's TCMB exchange rates and send them via Apprise.
    ---
    tags:
      - TCMB Proxy
    parameters:
      - name: X-API-Key
        in: header
        type: string
        required: true
        description: API Key for authentication
      - name: notification_url
        in: query
        type: string
        required: false
        description: Override the default Apprise notification/connection URL
      - name: apprise_api_url
        in: query
        type: string
        required: false
        description: Override the default Apprise API server URL
      - name: series
        in: query
        type: string
        required: false
        description: Comma-separated or list of series codes to fetch
    responses:
      200:
        description: Notification sent successfully
      400:
        description: Missing or invalid configuration
      502:
        description: Bad Gateway / Apprise Connection Error
      500:
        description: Internal Server Error or Upstream Error
    """
    if not TCMB_API_KEY:
        return Response("TCMB_API_KEY not configured on server.", status=500)

    # Determine parameters from request (support JSON, Query, and Form)
    req_data = {}
    if request.is_json:
        try:
            req_data = request.get_json() or {}
        except Exception:
            pass

    notification_url = (
        req_data.get("notification_url")
        or request.args.get("notification_url")
        or request.form.get("notification_url")
        or APPRISE_NOTIFICATION_URL
    )

    apprise_api_url = (
        req_data.get("apprise_api_url")
        or request.args.get("apprise_api_url")
        or request.form.get("apprise_api_url")
        or APPRISE_API_URL
    )

    if not apprise_api_url:
        return jsonify({
            "error": "Bad Request",
            "message": "Apprise API URL is not configured. Set APPRISE_API_URL environment variable or pass apprise_api_url."
        }), 400

    if not notification_url:
        return jsonify({
            "error": "Bad Request",
            "message": "Notification connection URL is not configured. Set APPRISE_NOTIFICATION_URL environment variable or pass notification_url."
        }), 400

    # Default series: USD and EUR Buying/Selling
    default_series = ["TP.DK.USD.A.YTL", "TP.DK.USD.S.YTL", "TP.DK.EUR.A.YTL", "TP.DK.EUR.S.YTL"]

    series_input = (
        req_data.get("series")
        or request.args.get("series")
        or request.form.get("series")
    )

    if series_input:
        if isinstance(series_input, str):
            if "," in series_input:
                series_list = [s.strip() for s in series_input.split(",") if s.strip()]
            elif "-" in series_input:
                series_list = [s.strip() for s in series_input.split("-") if s.strip()]
            else:
                series_list = [series_input.strip()]
        elif isinstance(series_input, list):
            series_list = [str(s).strip() for s in series_input if str(s).strip()]
        else:
            series_list = default_series
    else:
        series_list = default_series

    # Query last 5 days to ensure we get data even on weekends and holidays
    today = datetime.now()
    start_date = today - timedelta(days=5)
    startDate_str = start_date.strftime("%d-%m-%Y")
    endDate_str = today.strftime("%d-%m-%Y")

    series_query = "-".join(series_list)
    params = {
        "series": series_query,
        "startDate": startDate_str,
        "endDate": endDate_str,
        "type": "json"
    }

    query_string = urlencode(params)
    target_url = f"{TCMB_BASE_URL}{query_string}"

    try:
        upstream_response = requests.get(
            target_url, headers={"Key": TCMB_API_KEY}, timeout=30
        )
        upstream_response.raise_for_status()
        data = upstream_response.json()
    except Exception as e:
        return jsonify({
            "error": "Upstream Error",
            "message": f"Error fetching rates from TCMB: {str(e)}"
        }), 502

    items = data.get("items", [])
    if not items:
        return jsonify({
            "error": "Not Found",
            "message": "No data returned from TCMB EVDS."
        }), 500

    # Forward-fill logic
    last_known_values = {}
    processed_items = []

    for item in items:
        new_item = item.copy()
        for key, value in item.items():
            if value is not None:
                if key.startswith("TP"):
                    try:
                        value = round(float(value), 4)
                        new_item[key] = value
                    except (ValueError, TypeError):
                        pass
                last_known_values[key] = value
            elif key in last_known_values:
                new_item[key] = last_known_values[key]
        processed_items.append(new_item)

    latest_item = processed_items[-1]
    date_str = latest_item.get("Tarih", today.strftime("%d-%m-%Y"))

    SERIES_NAMES = {
        "TP_DK_USD_A_YTL": "USD Buying",
        "TP_DK_USD_S_YTL": "USD Selling",
        "TP_DK_EUR_A_YTL": "EUR Buying",
        "TP_DK_EUR_S_YTL": "EUR Selling",
        "TP_DK_USD_A": "USD Buying",
        "TP_DK_USD_S": "USD Selling",
        "TP_DK_EUR_A": "EUR Buying",
        "TP_DK_EUR_S": "EUR Selling",
    }

    rates_text = []
    for series in series_list:
        key_name = series.replace(".", "_")
        if key_name in latest_item:
            val = latest_item[key_name]
            friendly_name = SERIES_NAMES.get(key_name, series)
            if val is not None:
                rates_text.append(f"- {friendly_name}: {val} TRY")
            else:
                rates_text.append(f"- {friendly_name}: N/A")

    if not rates_text:
        return jsonify({
            "error": "Process Error",
            "message": "No exchange rates found in the response."
        }), 500

    title = f"TCMB Exchange Rates ({date_str})"
    body = "\n".join(rates_text)

    apprise_payload = {
        "urls": notification_url,
        "title": title,
        "body": body,
        "type": "info"
    }

    try:
        apprise_api_endpoint = f"{apprise_api_url.rstrip('/')}/notify/"
        apprise_response = requests.post(
            apprise_api_endpoint,
            json=apprise_payload,
            headers={"Accept": "application/json"},
            timeout=15
        )

        apprise_data = {}
        if "application/json" in apprise_response.headers.get("Content-Type", "").lower():
            try:
                apprise_data = apprise_response.json()
            except ValueError:
                pass

        if apprise_response.status_code not in (200, 201):
            return jsonify({
                "error": "Apprise Notification Failed",
                "status_code": apprise_response.status_code,
                "message": apprise_response.text,
                "details": apprise_data
            }), 502

        return jsonify({
            "status": "success",
            "message": "Notification sent successfully via Apprise.",
            "title": title,
            "body": body,
            "apprise_response": apprise_data or apprise_response.text
        })

    except Exception as e:
        return jsonify({
            "error": "Apprise Connection Error",
            "message": f"Could not connect to Apprise API at {apprise_api_url}: {str(e)}"
        }), 502

def has_notified_today():
    state_file = ".notified_today"
    today_str = datetime.now().strftime("%d-%m-%Y")
    if os.path.exists(state_file):
        try:
            with open(state_file, "r") as f:
                if f.read().strip() == today_str:
                    return True
        except Exception:
            pass
    return False

def mark_notified_today():
    state_file = ".notified_today"
    today_str = datetime.now().strftime("%d-%m-%Y")
    try:
        with open(state_file, "w") as f:
            f.write(today_str)
    except Exception:
        pass

def fetch_and_notify_full_rates():
    if not TCMB_API_KEY or not APPRISE_API_URL or not APPRISE_NOTIFICATION_URL:
        return

    # Default series: USD and EUR Buying/Selling
    series_list = ["TP.DK.USD.A.YTL", "TP.DK.USD.S.YTL", "TP.DK.EUR.A.YTL", "TP.DK.EUR.S.YTL"]
    today = datetime.now()
    start_date = today - timedelta(days=5)
    startDate_str = start_date.strftime("%d-%m-%Y")
    endDate_str = today.strftime("%d-%m-%Y")

    series_query = "-".join(series_list)
    params = {
        "series": series_query,
        "startDate": startDate_str,
        "endDate": endDate_str,
        "type": "json"
    }

    query_string = urlencode(params)
    target_url = f"{TCMB_BASE_URL}{query_string}"

    try:
        upstream_response = requests.get(
            target_url, headers={"Key": TCMB_API_KEY}, timeout=20
        )
        upstream_response.raise_for_status()
        data = upstream_response.json()
    except Exception:
        return

    items = data.get("items", [])
    if not items:
        return

    # Forward-fill logic
    last_known_values = {}
    processed_items = []

    for item in items:
        new_item = item.copy()
        for key, value in item.items():
            if value is not None:
                if key.startswith("TP"):
                    try:
                        value = round(float(value), 4)
                        new_item[key] = value
                    except (ValueError, TypeError):
                        pass
                last_known_values[key] = value
            elif key in last_known_values:
                new_item[key] = last_known_values[key]
        processed_items.append(new_item)

    latest_item = processed_items[-1]
    date_str = latest_item.get("Tarih", today.strftime("%d-%m-%Y"))

    SERIES_NAMES = {
        "TP_DK_USD_A_YTL": "USD Buying",
        "TP_DK_USD_S_YTL": "USD Selling",
        "TP_DK_EUR_A_YTL": "EUR Buying",
        "TP_DK_EUR_S_YTL": "EUR Selling",
    }

    rates_text = []
    for series in series_list:
        key_name = series.replace(".", "_")
        if key_name in latest_item:
            val = latest_item[key_name]
            friendly_name = SERIES_NAMES.get(key_name, series)
            if val is not None:
                rates_text.append(f"- {friendly_name}: {val} TRY")

    if not rates_text:
        return

    title = f"TCMB Exchange Rates ({date_str})"
    body = "\n".join(rates_text)

    apprise_payload = {
        "urls": APPRISE_NOTIFICATION_URL,
        "title": title,
        "body": body,
        "type": "info"
    }

    try:
        apprise_api_endpoint = f"{APPRISE_API_URL.rstrip('/')}/notify/"
        requests.post(
            apprise_api_endpoint,
            json=apprise_payload,
            headers={"Accept": "application/json"},
            timeout=15
        )
    except Exception:
        pass

def trigger_automated_notification():
    if not has_notified_today():
        mark_notified_today()
        fetch_and_notify_full_rates()

def trigger_automated_notification_async():
    thread = threading.Thread(target=trigger_automated_notification)
    thread.daemon = True
    thread.start()

def send_server_up_notification():
    if not APPRISE_API_URL or not APPRISE_NOTIFICATION_URL:
        return

    title = "HKD Proxy Status"
    body = "HKD Proxy Server is up and running!"

    apprise_payload = {
        "urls": APPRISE_NOTIFICATION_URL,
        "title": title,
        "body": body,
        "type": "success"
    }

    try:
        apprise_api_endpoint = f"{APPRISE_API_URL.rstrip('/')}/notify/"
        requests.post(
            apprise_api_endpoint,
            json=apprise_payload,
            headers={"Accept": "application/json"},
            timeout=15
        )
    except Exception:
        pass

def trigger_startup_tasks():
    send_server_up_notification()
    trigger_automated_notification()

def trigger_startup_tasks_async():
    thread = threading.Thread(target=trigger_startup_tasks)
    thread.daemon = True
    thread.start()

