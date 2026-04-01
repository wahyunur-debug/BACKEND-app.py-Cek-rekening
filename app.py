from flask import Flask, render_template, request, jsonify
import requests
import pandas as pd
import os

app = Flask(__name__)

API_KEY = os.getenv("XENDIT_API_KEY")


def cek_rekening(bank_code, account_number):
    url = "https://api.xendit.co/bank_accounts"

    try:
        response = requests.get(
            url,
            auth=(API_KEY, ""),
            params={
                "bank_code": bank_code,
                "account_number": account_number
            },
            timeout=10
        )

        if response.status_code != 200:
            return {}

        return response.json()

    except Exception as e:
        print("ERROR API:", e)
        return {}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    try:
        if "file" not in request.files:
            return jsonify({"error": "File tidak ditemukan"}), 400

        file = request.files["file"]

        df = pd.read_excel(file)

        # normalize kolom
        df.columns = [col.lower() for col in df.columns]

        required_cols = ["nama", "rekening", "bank"]
        for col in required_cols:
            if col not in df.columns:
                return jsonify({"error": f"Kolom {col} tidak ada"}), 400

        results = []

        for _, row in df.iterrows():
            nama = str(row["nama"]).strip()
            rekening = str(row["rekening"]).strip()
            bank = str(row["bank"]).strip().upper()

            res = cek_rekening(bank, rekening)

            nama_bank = res.get("account_holder_name", "")

            if not nama_bank:
                status = "INVALID"
            elif nama.upper() == nama_bank.upper():
                status = "MATCH"
            elif nama.upper() in nama_bank.upper():
                status = "MIRIP"
            else:
                status = "SALAH"

            results.append({
                "nama": nama,
                "rekening": rekening,
                "bank": bank,
                "nama_bank": nama_bank,
                "status": status
            })

        return jsonify(results)

    except Exception as e:
        print("ERROR:", e)
        return jsonify({"error": "Terjadi kesalahan saat proses file"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
