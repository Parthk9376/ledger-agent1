
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "running"})

@app.route("/answer", methods=["POST"])
def answer():
    data = request.get_json(silent=True) or {}
    question = data.get("question", "")

    if not question:
        return jsonify({"error": "Missing question"}), 400

    return jsonify({"answer": "Ledger API setup is working"})
