from flask import Flask, render_template, request, jsonify
import requests
import pandas as pd
import os
import time
from dotenv import load_dotenv

# ✅ Optional (buat local aja, di Railway aman walau ga ada)
load_dotenv()

app = Flask(__name__)

# ✅ Ambil dari Railway (utama) + fallback .env
API_KEY = os.environ.get("XENDIT_API_KEY")

print("API KEY TERBACA:", "ADA" if API_KEY else "TIDAK ADA")

def validasi_format_rekening(rekening):
    if not rekening.isdigit():
        return False, "Nomor rekening harus berupa angka"
    if len(rekening) < 6 or len(rekening) > 20:
        return False, "Panjang nomor rekening tidak wajar"
    return True, ""

def cek_rekening(bank_code, account_number):
    if not API_KEY:
        return {"error": "API key tidak ditemukan"}

    url = "https://api.xendit.co/bank_accounts/inquiries"
    try:
        response = requests.post(
            url,
            auth=(API_KEY, ""),
            json={
                "bank_code": bank_code,
                "account_number": account_number
            },
            timeout=10
        )

        print("STATUS:", response.status_code)
        print("RESPONSE:", response.text)

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
        print("UPLOAD HIT")

        if not API_KEY:
            return jsonify({"error": "XENDIT_API_KEY belum diset di Railway"}), 500

        if "file" not in request.files:
            return jsonify({"error": "File tidak ditemukan"}), 400

        file = request.files["file"]

        try:
            df = pd.read_excel(file)
        except Exception:
            return jsonify({"error": "File Excel tidak valid"}), 400

        df.columns = [col.lower().strip() for col in df.columns]

        required_cols = ["nama", "rekening", "bank"]
        for col in required_cols:
            if col not in df.columns:
                return jsonify({"error": f"Kolom '{col}' tidak ditemukan"}), 400

        results = []

        for _, row in df.iterrows():
            nama = str(row["nama"]).strip()
            rekening = str(row["rekening"]).strip()

            if rekening.endswith(".0"):
                rekening = rekening[:-2]

            bank = str(row["bank"]).strip().upper()

            valid, pesan = validasi_format_rekening(rekening)
            if not valid:
                results.append({
                    "nama": nama,
                    "rekening": rekening,
                    "bank": bank,
                    "nama_bank": "",
                    "status": "FORMAT SALAH",
                    "keterangan": pesan
                })
                continue

            res = cek_rekening(bank, rekening)
            nama_bank = res.get("account_holder_name", "")

            if not nama_bank:
                status = "INVALID"
                keterangan = "Rekening tidak ditemukan"
            elif nama.upper() == nama_bank.upper():
                status = "MATCH"
                keterangan = "Nama sesuai"
            elif nama.upper() in nama_bank.upper():
                status = "MIRIP"
                keterangan = "Nama mirip"
            else:
                status = "SALAH"
                keterangan = f"Nama di bank: {nama_bank}"

            results.append({
                "nama": nama,
                "rekening": rekening,
                "bank": bank,
                "nama_bank": nama_bank,
                "status": status,
                "keterangan": keterangan
            })

            time.sleep(0.3)

        return jsonify(results)

    except Exception as e:
        print("ERROR FATAL:", e)
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    print("APP STARTED")
    app.run(host="0.0.0.0", port=5000)
