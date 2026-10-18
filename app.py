
import os
import re
import json
import logging
from datetime import datetime
from urllib.parse import urljoin

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

DATA_URL = os.environ.get("LEDGER_DATA_URL", "").strip()
TIMEOUT = 20
MAX_PAGES = 100

session = requests.Session()
session.headers.update({"Accept": "application/json"})


def norm(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def records_in(obj):
    """Extract record lists from common JSON response structures."""
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            return obj

        result = []
        for item in obj:
            result.extend(records_in(item))
        return result

    if isinstance(obj, dict):
        preferred = (
            "records", "orders", "transactions", "ledger",
            "items", "results", "rows", "data"
        )

        for key in preferred:
            value = obj.get(key)
            if isinstance(value, list):
                if not value or all(isinstance(x, dict) for x in value):
                    return value

                found = records_in(value)
                if found:
                    return found

        for value in obj.values():
            if isinstance(value, (dict, list)):
                found = records_in(value)
                if found:
                    return found

    return []


def fetch_ledger():
    if not DATA_URL:
        raise RuntimeError(
            "Missing LEDGER_DATA_URL in Render Environment"
        )

    url = DATA_URL
    params = None
    visited = set()
    records = []

    for _ in range(MAX_PAGES):
        if url in visited:
            break

        visited.add(url)

        response = session.get(
            url, params=params, timeout=TIMEOUT
        )
        response.raise_for_status()
        payload = response.json()
        params = None

        page_records = records_in(payload)
        records.extend(page_records)

        if not isinstance(payload, dict):
            break

        # Explicit next-page links
        next_url = payload.get("next")

        if not next_url:
            links = payload.get("links")
            if isinstance(links, dict):
                next_url = links.get("next")

        if isinstance(next_url, str) and next_url:
            url = urljoin(response.url, next_url)
            continue

        # Common pagination metadata
        pagination = payload.get("pagination")
        if not isinstance(pagination, dict):
            pagination = payload.get("meta", {})

        if isinstance(pagination, dict):
            current = pagination.get(
                "page", pagination.get("current_page")
            )
            last = pagination.get(
                "total_pages", pagination.get("last_page")
            )

            if current is not None and last is not None:
                if int(current) < int(last):
                    params = {"page": int(current) + 1}
                    url = DATA_URL
                    continue

        break

    # Deduplicate identical records
    unique = {}
    for record in records:
        key = json.dumps(
            record, sort_keys=True, default=str
        )
        unique[key] = record

    if not unique:
        raise RuntimeError(
            "The API returned no ledger records. "
            "Check the upstream response and pagination."
        )

    return list(unique.values())


def field(record, aliases):
    """Find a field by normalized name."""
    wanted = {norm(alias) for alias in aliases}

    for key, value in record.items():
        if norm(key) in wanted:
            return value

    return None


def number(value):
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return None

    negative = text.startswith("(") and text.endswith(")")
    text = text.replace(",", "")
    text = re.sub(r"[$₹€£]", "", text)
    text = re.sub(r"\bUSD\b", "", text, flags=re.I)
    text = text.strip(" ()")

    try:
        result = float(text)
        return -result if negative else result
    except ValueError:
        return None


def parse_date(value):
    if value is None:
        return None

    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value)
        except (ValueError, OSError, OverflowError):
            return None

    text = str(value).strip()

    try:
        return datetime.fromisoformat(
            text.replace("Z", "+00:00")
        )
    except ValueError:
        pass

    for fmt in (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d-%m-%Y",
        "%m/%d/%Y",
        "%Y-%m-%d %H:%M:%S",
        "%B %d, %Y"
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue

    return None


REGION_FIELDS = (
    "region", "sales_region", "territory",
    "sales_territory", "customer_region"
)

DATE_FIELDS = (
    "order_date", "date", "created_at",
    "transaction_date", "purchase_date",
    "paid_at", "timestamp"
)

AMOUNT_FIELDS = (
    "order_value_usd", "paid_amount_usd",
    "total_amount_usd", "amount_usd",
    "order_total", "order_value", "total_amount",
    "total", "amount", "sales", "revenue",
    "net_amount", "grand_total", "value"
)

STATUS_FIELDS = (
    "payment_status", "status", "order_status",
    "payment_state", "financial_status"
)

CURRENCY_FIELDS = ("currency", "currency_code")


def month_year(question):
    match = re.search(
        r"\b(january|february|march|april|may|june|"
        r"july|august|september|october|november|december)"
        r"\s+(20\d{2})\b",
        question.lower()
    )

    if not match:
        return None, None

    month = datetime.strptime(match.group(1), "%B").month
    return month, int(match.group(2))


def answer_question(question, records):
    q = question.lower()

    # Determine requested region
    region_match = re.search(
        r"\b(north|south|east|west|central)\b", q
    )
    region = region_match.group(1) if region_match else None

    month, year = month_year(q)

    # Select the metric
    is_average = bool(
        re.search(r"\b(average|avg|mean)\b", q)
    )
    asks_refund = "refund" in q
    asks_product = "product" in q
    asks_customer = "customer" in q
    asks_paid = bool(
        re.search(r"\b(paid|payment|settled)\b", q)
    )

    # Determine available fields from the actual data
    sample = records[0]

    region_key = next(
        (k for k in sample if norm(k) in
         {norm(x) for x in REGION_FIELDS}), None
    )
    date_key = next(
        (k for k in sample if norm(k) in
         {norm(x) for x in DATE_FIELDS}), None
    )
    amount_key = next(
        (k for k in sample if norm(k) in
         {norm(x) for x in AMOUNT_FIELDS}), None
    )
    status_key = next(
        (k for k in sample if norm(k) in
         {norm(x) for x in STATUS_FIELDS}), None
    )
    currency_key = next(
        (k for k in sample if norm(k) in
         {norm(x) for x in CURRENCY_FIELDS}), None
    )

    if not amount_key:
        raise ValueError(
            "Amount field not recognized. "
            "Inspect the actual ledger JSON schema."
        )

    selected = []

    for row in records:
        if region:
            value = field(row, REGION_FIELDS)
            if value is None or str(value).strip().lower() != region:
                continue

        if month is not None and year is not None:
            date_value = field(row, DATE_FIELDS)
            dt = parse_date(date_value)

            if dt is None or dt.year != year or dt.month != month:
                continue

        if asks_paid:
            status = field(row, STATUS_FIELDS)
            if status is None:
                raise ValueError(
                    "Paid-order question requires a status field."
                )

            if norm(status) not in {
                "paid", "settled", "completed", "succeeded"
            }:
                continue

        if asks_refund:
            refund_value = field(
                row, ("refund_amount", "refunded_amount", "refund")
            )
            if refund_value is None:
                continue

            amount = number(refund_value)
        else:
            amount = number(field(row, AMOUNT_FIELDS))

        if amount is None:
            continue

        if currency_key:
            currency = str(row.get(currency_key, "")).upper()
            if "USD" in q and currency not in ("USD", "US$", "$"):
                continue

        selected.append(amount)

    if not selected:
        raise ValueError(
            "No matching records found for the requested question."
        )

    if is_average:
        result = sum(selected) / len(selected)
    else:
        result = sum(selected)

    return f"{result:.2f}"


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "running",
        "service": "Ledger Agent",
        "answer_endpoint": "/answer"
    })


@app.route("/answer", methods=["POST"])
def answer():
    body = request.get_json(silent=True) or {}
    question = body.get("question")

    if not isinstance(question, str) or not question.strip():
        return jsonify({"error": "Missing question"}), 400

    try:
        records = fetch_ledger()
        result = answer_question(question, records)
        return jsonify({"answer": result})

    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response else 502
        app.logger.exception("Upstream ledger HTTP error")
        return jsonify({
            "error": "Ledger data request failed",
            "upstream_status": status
        }), 502

    except Exception as exc:
        app.logger.exception("Ledger processing failed")
        return jsonify({
            "error": str(exc),
            "type": type(exc).__name__
        }), 500


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000"))
    )
