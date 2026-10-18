
import os
import re
import json
import requests
from datetime import datetime
from flask import Flask, request, jsonify

app = Flask(__name__)

DATA_URL = os.environ.get("LEDGER_DATA_URL", "").strip()
TIMEOUT = 8


def get_json(url, params=None):
    response = requests.get(
        url,
        params=params,
        timeout=TIMEOUT
    )
    response.raise_for_status()
    return response.json()


def find_records(obj):
    """Find record arrays in common JSON API response formats."""
    if isinstance(obj, list):
        if all(isinstance(x, dict) for x in obj):
            return obj
        for item in obj:
            result = find_records(item)
            if result:
                return result

    if isinstance(obj, dict):
        for key in (
            "records", "orders", "data", "results",
            "items", "transactions", "ledger", "rows"
        ):
            value = obj.get(key)
            if isinstance(value, list):
                if not value or isinstance(value[0], dict):
                    return value
                result = find_records(value)
                if result:
                    return result

        for value in obj.values():
            if isinstance(value, (dict, list)):
                result = find_records(value)
                if result:
                    return result

    return []


def fetch_all():
    if not DATA_URL:
        raise ValueError("LEDGER_DATA_URL is not configured")

    url = DATA_URL
    params = None
    all_records = []
    seen = set()

    for _ in range(100):
        if url in seen:
            break
        seen.add(url)

        response = requests.get(
            url, params=params, timeout=TIMEOUT
        )
        response.raise_for_status()
        payload = response.json()
        params = None

        records = find_records(payload)
        all_records.extend(records)

        if not isinstance(payload, dict):
            break

        # Common pagination formats
        next_value = payload.get("next")
        if isinstance(next_value, str) and next_value:
            url = requests.compat.urljoin(url, next_value)
            continue

        links = payload.get("links", {})
        if isinstance(links, dict) and links.get("next"):
            url = requests.compat.urljoin(url, links["next"])
            continue

        page_info = payload.get("pagination", {})
        if isinstance(page_info, dict) and page_info.get("next"):
            url = requests.compat.urljoin(url, page_info["next"])
            continue

        # Common page-number pagination
        meta = payload.get("meta", {})
        if isinstance(meta, dict):
            current = meta.get("page")
            last = meta.get("total_pages")
            if current is not None and last is not None:
                if int(current) < int(last):
                    params = {"page": int(current) + 1}
                    url = DATA_URL
                    continue

        break

    # Remove duplicate records where possible
    unique = {}
    for record in all_records:
        key = json.dumps(record, sort_keys=True, default=str)
        unique[key] = record

    return list(unique.values())


def normalize(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def get_field(record, names):
    wanted = {normalize(n) for n in names}

    for key, value in record.items():
        if normalize(key) in wanted:
            return value

    return None


def parse_date(value):
    if value is None:
        return None

    text = str(value).strip()

    try:
        return datetime.fromisoformat(
            text.replace("Z", "+00:00")
        )
    except ValueError:
        pass

    for fmt in (
        "%Y-%m-%d", "%Y/%m/%d",
        "%d-%m-%Y", "%m/%d/%Y",
        "%Y-%m-%d %H:%M:%S"
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue

    return None


def answer_question(question, records):
    q = question.lower()

    if not records:
        raise ValueError("No ledger records were found")

    # Identify the requested region
    region = None
    match = re.search(
        r"\b(north|south|east|west|central)\b", q
    )
    if match:
        region = match.group(1)

    # Identify the requested month and year
    month_match = re.search(
        r"\b(january|february|march|april|may|june|"
        r"july|august|september|october|november|december)"
        r"\s+(20\d{2})\b", q
    )

    month_number = None
    year = None

    if month_match:
        month_number = datetime.strptime(
            month_match.group(1), "%B"
        ).month
        year = int(month_match.group(2))

    # Discover likely field names from actual records
    sample = records[0]

    date_keys = [
        "date", "order_date", "created_at",
        "transaction_date", "timestamp"
    ]
    region_keys = [
        "region", "sales_region", "territory"
    ]
    amount_keys = [
        "revenue", "total_revenue", "amount",
        "total", "order_total", "sales",
        "net_amount", "total_amount"
    ]

    date_field = next(
        (k for k in sample if normalize(k) in
         {normalize(x) for x in date_keys}), None
    )
    region_field = next(
        (k for k in sample if normalize(k) in
         {normalize(x) for x in region_keys}), None
    )
    amount_field = next(
        (k for k in sample if normalize(k) in
         {normalize(x) for x in amount_keys}), None
    )

    if not amount_field:
        raise ValueError(
            "Could not identify the amount field. "
            "Inspect the ledger JSON to map the correct field."
        )

    selected = []

    for row in records:
        if region and region_field:
            value = str(row.get(region_field, "")).lower()
            if value != region:
                continue

        if month_number and year and date_field:
            dt = parse_date(row.get(date_field))
            if not dt or dt.year != year or dt.month != month_number:
                continue

        selected.append(row)

    # Sum the selected monetary values
    total = 0.0

    for row in selected:
        value = row.get(amount_field)
        if value is None:
            continue

        try:
            total += float(
                str(value).replace(",", "").replace("$", "")
            )
        except (ValueError, TypeError):
            continue

    return f"{total:.2f}"


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "running",
        "service": "Ledger Agent",
        "answer_endpoint": "/answer"
    })


@app.route("/answer", methods=["POST"])
def answer():
    data = request.get_json(silent=True) or {}
    question = data.get("question")

    if not isinstance(question, str) or not question.strip():
        return jsonify({"error": "Please provide a question"}), 400

    try:
        records = fetch_all()
        result = answer_question(question, records)
        return jsonify({"answer": result})

    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response else 502
        app.logger.exception("Ledger API HTTP error")
        return jsonify({
            "error": "Could not retrieve ledger data",
            "upstream_status": status
        }), 502

    except Exception:
        app.logger.exception("Ledger answer failed")
        return jsonify({
            "error": "Unable to calculate the answer. Check Render logs."
        }), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
