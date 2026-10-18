
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
TIMEOUT = 25
MAX_PAGES = 100

http = requests.Session()
http.headers.update({"Accept": "application/json"})


def norm(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def get_field(obj, aliases):
    if not isinstance(obj, dict):
        return None
    wanted = {norm(x) for x in aliases}
    for key, value in obj.items():
        if norm(key) in wanted:
            return value
    return None


def extract_records(obj):
    """Find likely record arrays in common API responses."""
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            return obj
        result = []
        for item in obj:
            result.extend(extract_records(item))
        return result

    if isinstance(obj, dict):
        for key in (
            "records", "orders", "transactions", "ledger",
            "items", "results", "rows"
        ):
            value = obj.get(key)
            if isinstance(value, list):
                if not value or all(isinstance(x, dict) for x in value):
                    return value
                result = extract_records(value)
                if result:
                    return result

        # A common API response is {"data": {"orders": [...]}}
        for key in ("data", "result", "response"):
            value = obj.get(key)
            if isinstance(value, (dict, list)):
                result = extract_records(value)
                if result:
                    return result

    return []


def fetch_records():
    if not DATA_URL:
        raise RuntimeError(
            "LEDGER_DATA_URL is missing from Render Environment"
        )

    url = DATA_URL
    params = None
    visited = set()
    all_records = []

    for _ in range(MAX_PAGES):
        if url in visited:
            break
        visited.add(url)

        response = http.get(url, params=params, timeout=TIMEOUT)
        response.raise_for_status()

        try:
            payload = response.json()
        except ValueError:
            raise RuntimeError(
                "The ledger API did not return valid JSON"
            )

        all_records.extend(extract_records(payload))
        params = None

        if not isinstance(payload, dict):
            break

        # Follow explicit pagination links when provided.
        next_url = payload.get("next")
        links = payload.get("links")

        if not next_url and isinstance(links, dict):
            next_url = links.get("next")

        if isinstance(next_url, str) and next_url:
            url = urljoin(response.url, next_url)
            continue

        # Support common page-number metadata.
        meta = payload.get("pagination")
        if not isinstance(meta, dict):
            meta = payload.get("meta", {})

        if isinstance(meta, dict):
            current = meta.get("page", meta.get("current_page"))
            last = meta.get("total_pages", meta.get("last_page"))

            if current is not None and last is not None:
                try:
                    if int(current) < int(last):
                        url = DATA_URL
                        params = {"page": int(current) + 1}
                        continue
                except (TypeError, ValueError):
                    pass

        break

    # Remove exact duplicate records.
    unique = {}
    for row in all_records:
        key = json.dumps(row, sort_keys=True, default=str)
        unique[key] = row

    records = list(unique.values())

    if not records:
        raise RuntimeError(
            "No ledger records found; inspect the API response schema"
        )

    app.logger.info("Loaded %d ledger records", len(records))
    return records


REGION_FIELDS = [
    "region", "sales_region", "territory", "sales_territory"
]
DATE_FIELDS = [
    "order_date", "date", "created_at",
    "transaction_date", "purchase_date", "timestamp"
]
STATUS_FIELDS = [
    "payment_status", "financial_status",
    "payment_state", "status"
]
CUSTOMER_FIELDS = [
    "customer_id", "customerId", "customer_email",
    "buyer_id", "customer_name", "customer"
]
AMOUNT_FIELDS = [
    "order_value_usd", "paid_amount_usd",
    "total_amount_usd", "amount_usd", "order_total",
    "order_value", "total_amount", "grand_total",
    "total", "amount", "net_amount", "revenue", "sales"
]
PRODUCT_FIELDS = [
    "product_name", "product", "item_name",
    "title", "sku", "description"
]


def parse_date(value):
    if value is None:
        return None

    text = str(value).strip()

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y",
                "%m/%d/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue

    return None


def parse_number(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (float, int)):
        return float(value)

    text = str(value).strip()
    if not text:
        return None

    negative = text.startswith("(") and text.endswith(")")
    text = text.replace(",", "")
    text = re.sub(r"[$£€₹]", "", text)
    text = re.sub(r"\bUSD\b", "", text, flags=re.I)
    text = text.strip(" ()")

    try:
        result = float(text)
        return -result if negative else result
    except ValueError:
        return None


def is_paid(row):
    status = get_field(row, STATUS_FIELDS)
    if status is None:
        return False

    return norm(status) in {
        "paid", "settled", "completed", "succeeded"
    }


def find_customer(row):
    value = get_field(row, CUSTOMER_FIELDS)

    if isinstance(value, dict):
        value = get_field(
            value, ["id", "customer_id", "email", "name"]
        )

    if value is None:
        nested = row.get("customer")
        if isinstance(nested, dict):
            value = get_field(nested, ["id", "customer_id", "email"])

    if value is None:
        return None

    value = str(value).strip().lower()
    return value or None


def product_matches(row, product_name):
    """Match a requested product against product/item records."""
    target = norm(product_name)

    # Check top-level product fields.
    value = get_field(row, PRODUCT_FIELDS)
    if value is not None and target in norm(value):
        return True

    # Check line-item arrays, without matching customer names.
    for key in ("items", "line_items", "products", "order_items"):
        items = row.get(key)
        if isinstance(items, dict):
            items = [items]

        if not isinstance(items, list):
            continue

        for item in items:
            if isinstance(item, dict):
                name = get_field(item, PRODUCT_FIELDS)
                if name is not None and target in norm(name):
                    return True
            elif isinstance(item, str) and target in norm(item):
                return True

    return False


def requested_product(question):
    # Prefer a known product name appearing in the question.
    match = re.search(
        r"\b(toaster|kettle|blender|microwave|refrigerator|"
        r"washer|oven|air conditioner)\b",
        question,
        flags=re.I
    )
    if match:
        return match.group(1)

    match = re.search(
        r"\b(?:bought|purchased|buy)\s+(?:a|an|the)\s+"
        r"([a-z0-9 -]+?)(?:\s*\(|\s+in\s+|\s+from\s+|\?|$)",
        question,
        flags=re.I
    )
    return match.group(1).strip() if match else None


def answer_question(question, records):
    q = question.lower()

    # Customer-count questions.
    if any(phrase in q for phrase in (
        "distinct customer", "unique customer",
        "how many customers"
    )):
        product = requested_product(q)
        if not product:
            raise ValueError("Could not identify the requested product")

        customers = set()

        for row in records:
            if not is_paid(row):
                continue
            if not product_matches(row, product):
                continue

            customer = find_customer(row)
            if customer:
                customers.add(customer)

        return str(len(customers))

    # Region filter.
    region_match = re.search(
        r"\b(north|south|east|west|central)\b", q
    )
    region = region_match.group(1) if region_match else None

    # Optional month/year filter.
    month_match = re.search(
        r"\b(january|february|march|april|may|june|july|"
        r"august|september|october|november|december)\s+(20\d{2})\b",
        q
    )
    month = year = None

    if month_match:
        month = datetime.strptime(
            month_match.group(1), "%B"
        ).month
        year = int(month_match.group(2))

    average = bool(re.search(r"\b(average|avg|mean)\b", q))
    paid_only = bool(re.search(r"\b(paid|settled)\b", q))

    amounts = []

    for row in records:
        if region:
            value = get_field(row, REGION_FIELDS)
            if value is None or str(value).strip().lower() != region:
                continue

        if month is not None:
            dt = parse_date(get_field(row, DATE_FIELDS))
            if dt is None or dt.month != month or dt.year != year:
                continue

        if paid_only and not is_paid(row):
            continue

        amount = parse_number(get_field(row, AMOUNT_FIELDS))
        if amount is None:
            continue

        amounts.append(amount)

    if not amounts:
        raise ValueError(
            "No matching monetary records. Check actual ledger fields."
        )

    result = sum(amounts) / len(amounts) if average else sum(amounts)
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
        records = fetch_records()
        result = answer_question(question, records)
        return jsonify({"answer": result})

    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response else 502
        app.logger.exception("Ledger API request failed")
        return jsonify({
            "error": "Ledger API request failed",
            "upstream_status": status
        }), 502

    except requests.RequestException:
        app.logger.exception("Ledger network request failed")
        return jsonify({
            "error": "Could not connect to the ledger API"
        }), 502

    except Exception:
        app.logger.exception("Ledger answer processing failed")
        return jsonify({
            "error": "Ledger processing failed; check Render logs"
        }), 500


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000"))
    )
