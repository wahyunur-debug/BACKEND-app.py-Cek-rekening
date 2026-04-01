from flask import Flask, render_template, request, jsonify
import requests
import pandas as pd
from io import BytesIO

app = Flask(__name__)

API_KEY = "ISI_API_KEY_XENDIT_LO"

def cek_rekening(bank_code, account_number):
    url = "https://api.xendit.co/bank_accounts"

    response = requests.get(
        url,
        auth=(API_KEY, ""),
        params={
            "bank_code": bank_code,
            "account_number": account_number
        }
    )

    return response.json()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    file = request.files["file"]
    df = pd.read_excel(file)

    results = []

    for _, row in df.iterrows():
        nama = str(row["nama"])
        rekening = str(row["rekening"])
        bank = str(row["bank"])

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


if __name__ == "__main__":
    app.run()
