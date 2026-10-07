"""
INNOBLOCK 2.0 starter backend.

Routes
  GET  /health      Is the server up, and can it reach the chain?
  POST /ai/decide   Ask an AI model for a decision (returns a demo answer if API_KEY is empty)
  POST /records     Hash a text, store the hash on-chain, save the full text in the database
  GET  /records     List saved records, so the frontend can verify each one on-chain

Run locally:  python app.py
On Render:    gunicorn app:app --timeout 120
"""
import hashlib
import json
import os
import sqlite3
import re

import psycopg
import requests
import math
from statistics import median
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS
from psycopg.rows import dict_row
from web3 import Web3
from werkzeug.exceptions import HTTPException
from decimal import Decimal
from flask_cors import CORS

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))  # reads backend/.env into environment variables

# ---------------------------------------------------------------- settings
REQUIRED = ["RPC_URL", "PRIVATE_KEY", "CONTRACT_ADDRESS"]
missing = [name for name in REQUIRED if not os.getenv(name)]
if missing:
    raise SystemExit(f"Missing in backend/.env: {', '.join(missing)}. "
                     "Copy .env.example to .env and fill it in.")

RPC_URL = os.getenv("RPC_URL")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
CONTRACT_ADDRESS = os.getenv("CONTRACT_ADDRESS")
EXPLORER_URL = os.getenv("EXPLORER_URL", "https://sepolia.etherscan.io").rstrip("/")
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "*")
DATABASE_URL = os.getenv("DATABASE_URL", "")  # empty = local SQLite file
SUPABASE_DB_URL = os.getenv("SUPABASE_DB_URL", "")  # Postgres connection string for the comparable_sales table

AI_API_KEY = os.getenv("API_KEY", "")
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
AI_MODEL = os.getenv("AI_MODEL", "")
AI_SYSTEM_PROMPT = os.getenv(
    "AI_SYSTEM_PROMPT",
    "You are a decision engine. Reply with one short decision and a one-line reason.",
)

# ---------------------------------------------------------------- app + chain
app = Flask(__name__)
CORS(app, origins=[FRONTEND_ORIGIN])  # lets the frontend (another domain) call this API

CORS(app, origins=[
    "http://localhost:8001",
    "http://127.0.0.1:8001"
])

w3 = Web3(Web3.HTTPProvider(RPC_URL))
account = w3.eth.account.from_key(PRIVATE_KEY)  # the backend's own burner wallet

with open(os.path.join(HERE, "abi.json")) as f:
    contract = w3.eth.contract(address=Web3.to_checksum_address(CONTRACT_ADDRESS), abi=json.load(f))

# The full text of each record lives in a database. Locally that's a SQLite file.
# When you deploy, set DATABASE_URL to a Postgres connection string (Neon / Supabase):
# Render's free disk is wiped whenever the service restarts, so SQLite would lose records.
SQLITE_PATH = os.path.join(HERE, "records.db")


def query(sql, params=()):
    """Run one SQL statement on Postgres (if DATABASE_URL is set) or SQLite.
    Write SQL with ? placeholders; returns the rows as a list of dicts."""
    if DATABASE_URL:
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            cursor = conn.execute(sql.replace("?", "%s"), params)
            return cursor.fetchall() if cursor.description else []
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        with conn:  # commits on success
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


query("CREATE TABLE IF NOT EXISTS records (id BIGINT PRIMARY KEY, text TEXT, hash TEXT, tx_hash TEXT)")
def _json_safe(row):
    """Postgres returns Decimal and date values; turn them into plain JSON types."""
    clean = {}
    for key, value in row.items():
        if isinstance(value, Decimal):
            value = int(value) if value == value.to_integral_value() else float(value)
        elif hasattr(value, "isoformat"):
            value = value.isoformat()
        clean[key] = value
    return clean


def get_comparable_sales(city=None, locality=None, property_type=None,
                         bedrooms=None, area_sqft=None, area_tolerance=0.3, limit=5):
    """Read comparable sales from Supabase. Every filter is optional.
    All user values go through %s placeholders; the SQL text itself is built only from fixed fragments.
    Results are ordered by closeness in area (when given), then most recent sale."""
    if not SUPABASE_DB_URL:
        raise RuntimeError("SUPABASE_DB_URL is not set in backend/.env.")

    where, params = [], []
    if city:
        where.append("LOWER(city) = LOWER(%s)")
        params.append(city)
    if locality:
        where.append("LOWER(locality) = LOWER(%s)")
        params.append(locality)
    if property_type:
        where.append("LOWER(property_type) = LOWER(%s)")
        params.append(property_type)
    if bedrooms is not None:
        where.append("bedrooms = %s")
        params.append(bedrooms)

    order, order_params = [], []
    if area_sqft is not None:
        where.append("area_sqft BETWEEN %s AND %s")
        params += [area_sqft * (1 - area_tolerance), area_sqft * (1 + area_tolerance)]
        order.append("ABS(area_sqft - %s)")
        order_params.append(area_sqft)
    order.append("sale_date DESC NULLS LAST")

    sql = "SELECT * FROM comparable_sales"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY " + ", ".join(order) + " LIMIT %s"
    limit = max(1, min(int(limit), 20))

    # prepare_threshold=None keeps this working through Supabase's connection pooler
    with psycopg.connect(SUPABASE_DB_URL, row_factory=dict_row,
                         prepare_threshold=None, connect_timeout=10) as conn:
        rows = conn.execute(sql, params + order_params + [limit]).fetchall()
    return [_json_safe(row) for row in rows]

MIN_COMPARABLES = 3               # fewer than this and we refuse to produce a number
BASELINE_COMPARABLE_LIMIT = 10    # most comparables used in one baseline


def _text_field(body, name, required=False):
    value = body.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValueError(f"{name} is required.")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text.")
    return value.strip()


def _number_field(body, name, required=False, whole=False, positive=False, maximum=None):
    value = body.get(name)
    if value is None or value == "":
        if required:
            raise ValueError(f"{name} is required.")
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number.")
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a number.")
    if whole and number != int(number):
        raise ValueError(f"{name} must be a whole number.")
    if positive and number <= 0:
        raise ValueError(f"{name} must be greater than 0.")
    if number < 0:
        raise ValueError(f"{name} cannot be negative.")
    if maximum is not None and number > maximum:
        raise ValueError(f"{name} cannot be more than {maximum}.")
    return int(number) if whole else number


def _clean_number(value):
    """Round to 2 decimals and drop a trailing .0 so JSON shows 8000, not 8000.0."""
    value = round(float(value), 2)
    return int(value) if value == int(value) else value



def find_baseline_comparables(subject):
    """Find comparable sales, widening the search when needed."""

    def fetch(locality, bedrooms, area_sqft):
        rows = get_comparable_sales(
            city=subject["city"],
            locality=locality,
            property_type=subject["property_type"],
            bedrooms=bedrooms,
            area_sqft=area_sqft,
            limit=BASELINE_COMPARABLE_LIMIT,
        )

        return [
            r for r in rows
            if float(r.get("price_per_sqft") or 0) > 0
        ]

    # 1. Try the requested locality with the original filters.
    rows = fetch(
        subject["locality"] or None,
        subject["bedrooms"],
        subject["area_sqft"],
    )
    scope = "locality" if subject["locality"] else "city"

    # 2. Widen to the city if there are too few matches.
    if len(rows) < MIN_COMPARABLES and subject["locality"]:
        rows = fetch(
            None,
            subject["bedrooms"],
            subject["area_sqft"],
        )
        scope = "city"

    # 3. If still short, relax the bedroom and area filters.
    if len(rows) < MIN_COMPARABLES:
        rows = fetch(None, None, None)
        scope = "city_broadened"

    return rows, scope

def compute_baseline(comparables, area_sqft):
    """Pure arithmetic: median price_per_sqft of the comparables x the subject area."""
    rates = [float(c["price_per_sqft"]) for c in comparables]
    median_rate = median(rates)
    return {
        "baseline_value": _clean_number(median_rate * area_sqft),
        "median_price_per_sqft": _clean_number(median_rate),
        "min_price_per_sqft": _clean_number(min(rates)),
        "max_price_per_sqft": _clean_number(max(rates)),
        "comparables_used": len(rates),
        "comparables": comparables,
    }

VALUATION_SYSTEM_PROMPT = (
    "You are a property valuation analyst. The backend gives you a subject property, a set of comparable "
    "sales it retrieved from a comparable-sales dataset, and a baseline value it calculated "
    "(median price per sq ft of the comparables x subject area). All prices are in Indian rupees (INR).\n"
    "Rules:\n"
    "1. Base your valuation only on the supplied comparable sales. Never invent, add or assume other "
    "comparable properties, prices or market data.\n"
    "2. Do not describe the records as verified or real-world transactions. Call them 'records in the "
    "supplied comparable-sales dataset'. If a record's source is DEMO_DATA (or contains 'demo'), say the "
    "evidence is demonstration data.\n"
    "3. You may adjust the baseline for differences between the subject and the comparables (area, "
    "bedrooms, age, floor, parking), using only fields present in the supplied data. Do not assume a "
    "missing field.\n"
    "4. Stay close to baseline_value. Do not deviate by more than 10%, and explain any difference using "
    "the supplied data.\n"
    "5. If the evidence is thin or inconsistent, say so in the explanation and lower the confidence.\n"
    "6. confidence is a number from 0 to 100 for how well the comparables support the estimate "
    "(count, similarity, price spread).\n"
    "7. Reply with ONE JSON object and nothing else: no markdown, no code fences, no extra text. "
    "Keys exactly: estimated_value (number, INR, no commas or symbols), confidence (number 0-100), "
    "comparable_properties_used (integer), baseline_value (number), "
    "explanation (string, 2 to 4 sentences)."
)

COMPARABLE_PROMPT_FIELDS = ("id", "locality", "city", "property_type", "bedrooms", "area_sqft", "price",
                            "price_per_sqft", "property_age_years", "floor", "total_floors",
                            "parking_spaces", "sale_date", "source")


class AIProviderError(Exception):
    """The AI provider could not be reached or returned an error."""


def _parse_subject(body):
    """Validate the subject-property fields (same rules as /valuation/baseline). Raises ValueError."""
    return {
        "city": _text_field(body, "city", required=True),
        "locality": _text_field(body, "locality"),
        "property_type": _text_field(body, "property_type", required=True),
        "bedrooms": _number_field(body, "bedrooms", whole=True, maximum=20),
        "area_sqft": _number_field(body, "area_sqft", required=True, positive=True),
        "property_age_years": _number_field(body, "property_age_years"),
        "floor": _number_field(body, "floor", whole=True),
        "parking_spaces": _number_field(body, "parking_spaces", whole=True),
    }


def _evidence_source(rows):
    """Describe where the comparables came from, from the rows' own `source` column."""
    sources = sorted({str(r["source"]) for r in rows if r.get("source")})
    is_demo = any("demo" in s.lower() for s in sources)
    if is_demo:
        note = ("Evidence comes from the supplied comparable_sales dataset, which contains demonstration "
                "data. These are not verified real-world transactions.")
    elif sources:
        note = "Evidence comes from the supplied comparable_sales dataset. Sources: " + ", ".join(sources) + "."
    else:
        note = "Evidence comes from the supplied comparable_sales dataset. No source was recorded for these rows."
    return {"dataset": "comparable_sales", "sources": sources, "is_demo_data": is_demo, "note": note}


def build_valuation_prompt(subject, stats, rows):
    """The user message: only facts calculated or retrieved by the backend."""
    subject_facts = {k: v for k, v in subject.items() if v is not None}
    baseline_facts = {k: stats[k] for k in ("baseline_value", "median_price_per_sqft", "min_price_per_sqft",
                                            "max_price_per_sqft", "comparables_used")}
    comparables = [{k: r.get(k) for k in COMPARABLE_PROMPT_FIELDS} for r in rows]
    return (
        "Subject property:\n" + json.dumps(subject_facts, indent=2) + "\n\n"
        "Backend baseline calculation:\n" + json.dumps(baseline_facts, indent=2) + "\n\n"
        "Comparable sales from the dataset (the only evidence you may use):\n"
        + json.dumps(comparables, indent=2) + "\n\n"
        f"Return the JSON object now. Use comparable_properties_used = {stats['comparables_used']} "
        f"and baseline_value = {stats['baseline_value']} exactly as given."
    )


def ask_ai_for_json(system_prompt, user_prompt):
    """Send one chat request with the existing AI settings and return the reply text."""
    try:
        response = requests.post(
            f"{AI_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {AI_API_KEY}"},
            json={"model": AI_MODEL, "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]},
            timeout=60,
        )
    except requests.RequestException as err:
        raise AIProviderError(f"Could not reach the AI provider: {err}")
    if not response.ok:
        raise AIProviderError(f"AI provider returned {response.status_code}: {response.text[:300]}")
    try:
        return (response.json()["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError, ValueError):
        raise AIProviderError("The AI provider's reply was not in the expected format.")


def _extract_json_object(text):
    """Parse the AI reply as a JSON object, tolerating code fences or stray text around it."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("the reply was not a JSON object.")
        data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("the reply was not a JSON object.")
    return data


def _ai_number(value, name):
    """Accept a real number (or a numeric string like '12,000,000'); reject anything else."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} is missing or not a number.")
    if isinstance(value, str):
        value = value.replace(",", "").replace("₹", "").strip()
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} is not a number.")
    if not math.isfinite(number):
        raise ValueError(f"{name} is not a finite number.")
    return number

def sha256_hex(text):
    """Fingerprint of the text. The frontend computes the same value with ethers.sha256."""
    return "0x" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def tx_url(tx_hash):
    """Explorer link for a transaction."""
    return f"{EXPLORER_URL}/tx/{tx_hash}"


def store_hash_on_chain(hash_hex):
    """Sign store(hash) with the backend wallet, send it, and wait until it is mined.
    Returns (record id, transaction hash)."""
    tx = contract.functions.store(hash_hex).build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address, "pending"),
    })
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.status != 1:
        raise RuntimeError(f"Transaction failed on-chain: {tx_url(w3.to_hex(tx_hash))}")
    event = contract.events.RecordStored().process_receipt(receipt)[0]
    return event["args"]["id"], w3.to_hex(tx_hash)


# ---------------------------------------------------------------- routes
@app.get("/health")
def health():
    """UptimeRobot pings this to keep the server awake. It also proves the RPC works."""
    return {
        "ok": True,
        "chainId": w3.eth.chain_id,
        "block": w3.eth.block_number,
        "wallet": account.address,
        "contract": contract.address,
    }


@app.post("/ai/decide")
def ai_decide():
    """Send the prompt to any OpenAI-compatible chat API and return its answer."""
    prompt = (request.get_json(silent=True) or {}).get("prompt", "").strip()
    if not prompt:
        return jsonify(error="prompt is required"), 400

    if not AI_API_KEY:  # demo mode: the rest of the flow still works without a key
        return {"decision": f"DEMO DECISION (no API_KEY set): approve \"{prompt[:80]}\"", "demo": True}

    response = requests.post(
        f"{AI_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {AI_API_KEY}"},
        json={"model": AI_MODEL, "messages": [
            {"role": "system", "content": AI_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]},
        timeout=60,
    )
    if not response.ok:
        return jsonify(error=f"AI provider returned {response.status_code}: {response.text[:300]}"), 502
    decision = response.json()["choices"][0]["message"]["content"].strip()
    return {"decision": decision, "demo": False}


@app.post("/records")
def create_record():
    """Hash the text, store the hash on-chain, then save the full text with its id."""
    text = (request.get_json(silent=True) or {}).get("text", "").strip()
    if not text:
        return jsonify(error="text is required"), 400

    record_hash = sha256_hex(text)
    record_id, tx_hash = store_hash_on_chain(record_hash)

    query("INSERT INTO records (id, text, hash, tx_hash) VALUES (?, ?, ?, ?) "
          "ON CONFLICT (id) DO UPDATE SET text = excluded.text, hash = excluded.hash, "
          "tx_hash = excluded.tx_hash",
          (record_id, text, record_hash, tx_hash))
    return {"id": record_id, "text": text, "hash": record_hash,
            "txHash": tx_hash, "explorerUrl": tx_url(tx_hash)}, 201


@app.get("/records")
def list_records():
    """All saved records, newest first."""
    rows = query("SELECT id, text, hash, tx_hash FROM records ORDER BY id DESC")
    return {"records": [{"id": r["id"], "text": r["text"], "hash": r["hash"],
                         "txHash": r["tx_hash"], "explorerUrl": tx_url(r["tx_hash"])} for r in rows]}

@app.get("/comparables")
def comparables():
    """Temporary test route: comparable sales from Supabase.
    Optional query params: city, locality, property_type, bedrooms, area_sqft, limit."""
    args = request.args
    try:
        bedrooms = int(args["bedrooms"]) if args.get("bedrooms") else None
        area = float(args["area_sqft"]) if args.get("area_sqft") else None
        limit = int(args.get("limit", 5))
    except ValueError:
        return jsonify(error="bedrooms and limit must be whole numbers, and area_sqft must be a number."), 400
    if area is not None and not area > 0:
        return jsonify(error="area_sqft must be greater than 0."), 400
    if not SUPABASE_DB_URL:
        return jsonify(error="SUPABASE_DB_URL is not set in backend/.env."), 503

    rows = get_comparable_sales(
        city=args.get("city"), locality=args.get("locality"),
        property_type=args.get("property_type"), bedrooms=bedrooms,
        area_sqft=area, limit=limit,
    )
    return {"comparables": rows}

@app.post("/valuation/baseline")
def valuation_baseline():
    """Temporary test route: median price-per-sqft baseline from comparable sales."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="Send a JSON body with at least city, property_type and area_sqft."), 400
    try:
        subject = {
            "city": _text_field(body, "city", required=True),
            "locality": _text_field(body, "locality"),
            "property_type": _text_field(body, "property_type", required=True),
            "bedrooms": _number_field(body, "bedrooms", whole=True, maximum=20),
            "area_sqft": _number_field(body, "area_sqft", required=True, positive=True),
            "property_age_years": _number_field(body, "property_age_years"),
            "floor": _number_field(body, "floor", whole=True),
            "parking_spaces": _number_field(body, "parking_spaces", whole=True),
        }
    except ValueError as err:
        return jsonify(error=str(err)), 400
    if not SUPABASE_DB_URL:
        return jsonify(error="SUPABASE_DB_URL is not set in backend/.env."), 503

    rows, scope = find_baseline_comparables(subject)
    if len(rows) < MIN_COMPARABLES:
        return jsonify(
            error=(f"Found {len(rows)} comparable sale(s) in {subject['city']} for this property type, "
                   f"bedroom count and area range. At least {MIN_COMPARABLES} are needed, "
                   "so no valuation was produced."),
            comparables_found=len(rows), minimum_required=MIN_COMPARABLES, subject=subject,
        ), 422

    result = compute_baseline(rows, subject["area_sqft"])
    result["match_scope"] = scope
    result["subject"] = subject
    return result


@app.post("/valuation/ai")
def valuation_ai():
    """Calculate a comparable-sales baseline, then evaluate it with AI."""
    body = request.get_json(silent=True)

    if not isinstance(body, dict):
        return jsonify(error="A JSON request body is required."), 400

    try:
        subject = _parse_subject(body)
    except ValueError as err:
        return jsonify(error=str(err)), 400

    if not SUPABASE_DB_URL:
        return jsonify(
            error="SUPABASE_DB_URL is not configured on the backend."
        ), 503

    try:
        rows, scope = find_baseline_comparables(subject)
    except Exception as err:
        app.logger.exception("Could not fetch comparable sales")
        return jsonify(error=f"Could not fetch comparable sales: {err}"), 500

    if len(rows) < MIN_COMPARABLES:
        return jsonify(
            error=(
                f"Found {len(rows)} comparable sales. "
                f"At least {MIN_COMPARABLES} are required."
            ),
            comparables_found=len(rows),
            minimum_required=MIN_COMPARABLES,
        ), 422

    # Step 1: Calculate the baseline from comparable property sales.
    stats = compute_baseline(rows, subject["area_sqft"])
    baseline = stats["baseline_value"]

    # Step 2: Use AI to evaluate the baseline and supporting evidence.
    if not AI_API_KEY or not AI_MODEL:
        return jsonify(
            error="AI is not configured. Set API_KEY and AI_MODEL on Render."
        ), 503

    prompt = build_valuation_prompt(subject, stats, rows)

    try:
        ai_text = ask_ai_for_json(VALUATION_SYSTEM_PROMPT, prompt)
        ai_result = _extract_json_object(ai_text)

        estimated_value = _ai_number(
            ai_result.get("estimated_value"), "estimated_value"
        )
        confidence = _ai_number(
            ai_result.get("confidence"), "confidence"
        )
        explanation = ai_result.get("explanation")

        if not isinstance(explanation, str) or not explanation.strip():
            raise ValueError("AI did not return a valid explanation.")

        if not 0 <= confidence <= 100:
            raise ValueError("AI confidence must be between 0 and 100.")

        # Enforce the prompt's maximum 10% adjustment.
        if not baseline * 0.9 <= estimated_value <= baseline * 1.1:
            estimated_value = baseline
            explanation = (
                "The AI estimate was outside the permitted 10% adjustment "
                "from the comparable-sales baseline, so the baseline is used. "
                + explanation
            )

    except (AIProviderError, ValueError) as err:
        app.logger.exception("AI valuation failed")
        return jsonify(error=f"AI valuation failed: {err}"), 502

    return jsonify(
        estimated_value=round(estimated_value, 2),
        baseline_value=baseline,
        confidence=round(confidence, 2),
        comparable_properties_used=len(rows),
        median_price_per_sqft=stats["median_price_per_sqft"],
        explanation=explanation,
        match_scope=scope,
        evidence=_evidence_source(rows),
        demo=bool(_evidence_source(rows)["is_demo_data"]),
    )

@app.errorhandler(Exception)
def handle_error(e):
    """Always answer with JSON and a readable message, never an HTML crash page."""
    if isinstance(e, HTTPException):
        return jsonify(error=e.description), e.code
    message = str(e)
    if "insufficient funds" in message.lower():
        message = f"The backend wallet {account.address} has no test tokens. Fund it from a faucet."
    app.logger.exception(e)
    return jsonify(error=message), 500


if __name__ == "__main__":
    app.run(port=int(os.getenv("PORT", "5000")), debug=True)
