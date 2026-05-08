"""
Rekening Validator Pro — Production Backend
Author: Senior Engineering Standard
Stack: Flask + SSE + ThreadPoolExecutor + Redis/JSON fallback
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import threading
import time
from decimal import Decimal, InvalidOperation
from typing import Optional

import pandas as pd
import requests
from flask import Flask, Response, jsonify, render_template, request, send_file
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ─────────────────────────────────────────────────────────────────────────────
# BOOTSTRAP
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("validator")

app = Flask(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
API_KEY   = os.getenv("APICOID_API_KEY", "")
ADMIN_PWD = os.getenv("ADMIN_SECRET", "admin123")

# Endpoint resmi api.co.id (GET + header x-api-co-id)
API_ENDPOINT = "https://use.api.co.id/validation/bank"

MAX_WORKERS     = 4
REQUEST_TIMEOUT = 30
CODES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codes.json")

# ─────────────────────────────────────────────────────────────────────────────
# QUOTA STORE  (Redis jika tersedia, fallback JSON lokal)
# ─────────────────────────────────────────────────────────────────────────────
_redis = None
_lock  = threading.RLock()

try:
    import redis as _redis_lib  # type: ignore
    _ru = os.getenv("REDIS_URL", "")
    if _ru:
        _redis = _redis_lib.from_url(_ru, decode_responses=True, socket_timeout=3)
        _redis.ping()
        log.info("Quota store: Redis ✓")
except Exception as exc:
    log.warning("Quota store: Redis tidak tersedia (%s) → pakai JSON lokal", exc)


def _codes_read() -> dict:
    try:
        if os.path.exists(CODES_FILE):
            with open(CODES_FILE, encoding="utf-8") as fh:
                return json.load(fh)
    except Exception:
        pass
    return {}


def _codes_write(data: dict) -> None:
    with open(CODES_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def quota_get(code: str) -> Optional[int]:
    code = code.strip().upper()
    if _redis:
        v = _redis.get(f"q:{code}")
        return int(v) if v is not None else None
    d = _codes_read()
    return int(d[code]) if code in d else None


def quota_set(code: str, val: int) -> None:
    code = code.strip().upper()
    if _redis:
        _redis.set(f"q:{code}", val)
        return
    with _lock:
        d = _codes_read()
        d[code] = val
        _codes_write(d)


def quota_decr(code: str) -> int:
    """Atomic decrement, floor 0. Returns new value."""
    code = code.strip().upper()
    if _redis:
        new = _redis.decr(f"q:{code}")
        if new < 0:
            _redis.set(f"q:{code}", 0)
            return 0
        return new
    with _lock:
        d = _codes_read()
        cur = int(d.get(code, 0))
        new = max(0, cur - 1)
        d[code] = new
        _codes_write(d)
        return new


def quota_add(code: str, amount: int) -> int:
    code = code.strip().upper()
    cur = quota_get(code) or 0
    quota_set(code, cur + amount)
    return cur + amount


def quota_list() -> list[dict]:
    if _redis:
        keys = sorted(_redis.keys("q:*"))
        return [{"code": k[2:], "quota": int(_redis.get(k) or 0)} for k in keys]
    d = _codes_read()
    return [{"code": k, "quota": int(v)} for k, v in sorted(d.items())]


# Health check: verifikasi API key bisa benar-benar validasi rekening
_api_health_ok: Optional[bool] = None
_api_health_msg: str = ""
_api_health_lock = threading.Lock()


def api_health_check(force: bool = False) -> tuple[bool, str]:
    """Cek konektivitas API dan validitas key. Tidak memblokir berdasarkan hasil validasi."""
    global _api_health_ok, _api_health_msg
    if not force and _api_health_ok is not None:
        return _api_health_ok, _api_health_msg

    with _api_health_lock:
        if not force and _api_health_ok is not None:
            return _api_health_ok, _api_health_msg

        if not API_KEY:
            _api_health_ok = False
            _api_health_msg = "APICOID_API_KEY belum di-set"
            return False, _api_health_msg

        sess = get_session()
        try:
            # Cek koneksi + validitas key via endpoint available banks
            resp = sess.get(
                API_ENDPOINT + "/available",
                headers={"x-api-co-id": API_KEY, "Accept": "application/json"},
                timeout=15,
            )
            log.info("[HEALTH] HTTP %d | %s", resp.status_code, resp.text[:150])

            if resp.status_code == 401:
                _api_health_ok = False
                _api_health_msg = "API Key tidak valid (401). Cek di dashboard api.co.id"
            elif resp.status_code == 402:
                _api_health_ok = False
                _api_health_msg = "Saldo api.co.id habis (402). Top-up di dashboard api.co.id"
            elif resp.status_code == 200:
                body = resp.json()
                if body.get("is_success"):
                    total = body.get("data", {}).get("total", 0) if isinstance(body.get("data"), dict) else len(body.get("data", []))
                    _api_health_ok = True
                    _api_health_msg = f"API aktif, {total} bank tersedia"
                else:
                    _api_health_ok = False
                    _api_health_msg = f"API Error: {body.get('message', 'Unknown')}"
            else:
                _api_health_ok = False
                _api_health_msg = f"HTTP {resp.status_code}"
        except Exception as exc:
            _api_health_ok = False
            _api_health_msg = f"Gagal koneksi ke api.co.id: {exc}"

    return _api_health_ok, _api_health_msg

# ─────────────────────────────────────────────────────────────────────────────
# HTTP SESSION (shared, connection-pooled)
# ─────────────────────────────────────────────────────────────────────────────
def _make_session() -> requests.Session:
    sess = requests.Session()
    retry = Retry(total=0)          # Kita handle retry manual agar lebih kontrol
    adapter = HTTPAdapter(
        pool_connections=MAX_WORKERS + 2,
        pool_maxsize=MAX_WORKERS + 2,
        max_retries=retry,
    )
    sess.mount("https://", adapter)
    sess.mount("http://",  adapter)
    return sess


# Session global — di-share antar thread (thread-safe untuk requests)
_SESSION: Optional[requests.Session] = None
_SESSION_LOCK = threading.Lock()


def get_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        with _SESSION_LOCK:
            if _SESSION is None:
                _SESSION = _make_session()
    return _SESSION


# ─────────────────────────────────────────────────────────────────────────────
# TEXT UTILITIES
# ─────────────────────────────────────────────────────────────────────────────
_WS = re.compile(r"\s+")


def normalize(s: str) -> str:
    """Hapus semua spasi & uppercase — untuk perbandingan nama."""
    return _WS.sub("", str(s)).upper().strip()


def sanitize_account(val) -> str:
    """
    Konversi nilai rekening dari Excel ke string digit bersih.
    Handle: float (12345.0), scientific (1.23E+10), string campur huruf.
    """
    s = str(val).strip()
    if not s or s.lower() == "nan":
        return ""
    # Scientific notation
    if "e" in s.lower():
        try:
            s = format(Decimal(s), "f")
        except InvalidOperation:
            pass
    # Strip desimal trailing (.0)
    if "." in s:
        s = s.split(".")[0]
    # Hanya digit
    return re.sub(r"\D", "", s)


def sanitize_bank(raw: str) -> tuple[str, str]:
    """
    Return (short, full) e.g. ("bca", "bank_bca")
    Menerima input: "BCA", "bca", "bank_bca", "BANK_BCA"
    """
    b = re.sub(r"\s+", "", raw.strip().lower())
    b = re.sub(r"[^a-z0-9_]", "", b)
    if b.startswith("bank_"):
        return b[5:], b
    return b, f"bank_{b}"


# ─────────────────────────────────────────────────────────────────────────────
# CORE API CALL
# ─────────────────────────────────────────────────────────────────────────────

class APIResult:
    """Typed result dari API call."""
    __slots__ = ("account_name", "is_valid", "score", "error", "is_system_error")

    def __init__(
        self,
        *,
        account_name: str = "",
        is_valid: bool = False,
        score: float = 0.0,
        error: str = "",
        is_system_error: bool = False,
    ):
        self.account_name   = account_name
        self.is_valid       = is_valid
        self.score          = score
        self.error          = error
        self.is_system_error = is_system_error

    @property
    def ok(self) -> bool:
        return not self.error


def _call_api(sess: requests.Session, bank_code: str, account_no: str, account_name: str) -> Optional[APIResult]:
    """
    GET https://use.api.co.id/validation/bank
    Return: APIResult jika definitif, None jika perlu coba format lain.
    """
    try:
        resp = sess.get(
            API_ENDPOINT,
            params={
                "bank_code":      bank_code,
                "account_number": account_no,
                "account_name":   account_name,
            },
            headers={
                "x-api-co-id": API_KEY,
                "Accept":      "application/json",
            },
            timeout=REQUEST_TIMEOUT,
        )
        log.info("[API] %s | %s | HTTP %d | %s", bank_code, account_no, resp.status_code, resp.text[:150])

        if resp.status_code == 401:
            return APIResult(error="API Key tidak valid (401)", is_system_error=True)
        if resp.status_code == 402:
            return APIResult(error="Saldo api.co.id habis (402). Top-up di dashboard.", is_system_error=True)
        if resp.status_code == 429:
            time.sleep(2)
            return None
        if resp.status_code != 200:
            return None

        body = resp.json()

        if not body.get("is_success"):
            msg = body.get("message", "")
            if any(kw in msg.lower() for kw in ("bank_code", "invalid", "not supported")):
                return None  # Format bank salah, coba format lain
            return APIResult(error=msg or "API Error")

        inner = body.get("data") or {}
        name   = inner.get("name") or inner.get("account_name")
        valid  = bool(inner.get("is_valid"))
        score  = float(inner.get("score") or 0)
        api_msg = inner.get("message") or body.get("message") or "Rekening tidak ditemukan"

        if valid or name:
            return APIResult(
                account_name=str(name or "").strip(),
                is_valid=valid,
                score=score,
            )

        # is_valid=false + name=null
        # Mengembalikan error spesifik dari API agar transparan
        return APIResult(error=api_msg, is_system_error=False)

    except requests.exceptions.Timeout:
        log.warning("[API] Timeout: %s %s", bank_code, account_no)
        return None
    except Exception as exc:
        log.warning("[API] Exception: %s", exc)
        return None


def check_account(account_no: str, bank_raw: str, account_name: str = "") -> APIResult:
    """
    Validasi rekening via api.co.id.
    Coba format: bank_bca → bca secara berurutan.
    """
    if not API_KEY:
        return APIResult(error="APICOID_API_KEY belum di-set!", is_system_error=True)

    sess = get_session()
    short, full = sanitize_bank(bank_raw)

    # Coba full format dulu (bank_bca), lalu short (bca)
    formats = [full, short] if full != short else [full]

    last_error = "Rekening tidak ditemukan"
    for fmt in formats:
        res = _call_api(sess, fmt, account_no, account_name)
        if res is None:
            continue  # Format ini gagal, coba berikutnya
        if res.is_system_error:
            return res
        if res.ok:
            return res
        last_error = res.error

    return APIResult(error=last_error)


# ─────────────────────────────────────────────────────────────────────────────
# ROW PROCESSOR
# ─────────────────────────────────────────────────────────────────────────────

def process_row(index: int, row: dict) -> dict:
    nama     = str(row.get("nama", "")).strip()
    rekening = sanitize_account(row.get("rekening", ""))
    bank     = str(row.get("bank", "")).strip()

    base = {
        "type":     "result",
        "index":    index + 1,
        "nama":     nama,
        "rekening": rekening,
        "bank":     bank,
    }

    if not rekening:
        return {**base, "nama_bank": "-", "hasil": "TIDAK VALID"}

    result = check_account(rekening, bank, nama)

    # ── Tentukan hasil ──────────────────────────────────────────────────────
    if not result.ok:
        if result.is_system_error:
            return {**base, "nama_bank": f"⚠ {result.error}", "hasil": "ERROR"}
        # Rekening definitif tidak ada, tampilkan pesan dari API agar transparan
        api_err = result.error if result.error else "Tidak Ditemukan"
        return {**base, "nama_bank": f"API: {api_err}", "hasil": "TIDAK VALID"}

    nama_bank = result.account_name

    # Kriteria MATCH:
    #   • is_valid=True dari API baru  ATAU
    #   • score ≥ 7.0                  ATAU
    #   • nama bersih identik (untuk endpoint lama yang return nama penuh)
    if result.is_valid or result.score >= 7.0 or normalize(nama) == normalize(nama_bank):
        return {**base, "nama_bank": nama_bank or nama.upper(), "hasil": "MATCH"}

    if nama_bank:
        return {**base, "nama_bank": nama_bank, "hasil": "TIDAK SAMA"}

    return {**base, "nama_bank": "-", "hasil": "TIDAK VALID"}


# ─────────────────────────────────────────────────────────────────────────────
# SSE STREAM GENERATOR
# ─────────────────────────────────────────────────────────────────────────────

def stream_generator(records: list[dict], code: str):
    """
    Server-Sent Events generator.
    Kirim 'start' → N×'result' → 'done'.
    Kuota dipotong per baris hasil valid (ERROR tidak dipotong).
    """
    total = len(records)
    yield f"data: {json.dumps({'type': 'start', 'total': total})}\n\n"

    processed = 0

    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            future_map = {
                pool.submit(process_row, i, row): i
                for i, row in enumerate(records)
            }

            for future in as_completed(future_map):
                try:
                    payload = future.result()
                except Exception as exc:
                    idx = future_map[future]
                    payload = {
                        "type": "result", "index": idx + 1,
                        "nama": "-", "rekening": "-", "bank": "-",
                        "nama_bank": f"Exception: {exc}", "hasil": "ERROR",
                    }

                # Potong kuota hanya untuk hasil non-ERROR
                if payload.get("hasil") != "ERROR":
                    sisa = quota_decr(code)
                else:
                    sisa = quota_get(code) or 0

                payload["sisa_kuota"] = sisa
                processed += 1

                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    except Exception as exc:
        yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
        return

    yield f"data: {json.dumps({'type': 'done', 'total': total, 'processed': processed})}\n\n"


# ─────────────────────────────────────────────────────────────────────────────
# FLASK ROUTES — Public
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/verify-code", methods=["POST"])
def verify_code():
    body = request.get_json(silent=True) or {}
    code = str(body.get("code", "")).strip().upper()

    if not code:
        return jsonify({"valid": False, "message": "Kode tidak boleh kosong"}), 400

    q = quota_get(code)
    if q is None:
        return jsonify({"valid": False, "message": "Kode tidak ditemukan"}), 403
    if q <= 0:
        return jsonify({"valid": False, "message": "Kuota habis. Hubungi admin untuk top-up."}), 403

    return jsonify({"valid": True, "quota": q})


@app.route("/stream", methods=["POST"])
def stream():
    code = str(request.form.get("code", "")).strip().upper()

    # Guard: validasi kode & kuota sebelum baca file
    q = quota_get(code)
    if q is None:
        return jsonify({"error": "Kode tidak valid"}), 403
    if q <= 0:
        return jsonify({"error": "Kuota habis. Hubungi admin untuk top-up."}), 403

    # Pre-flight: cek apakah API key bisa validasi rekening
    healthy, health_msg = api_health_check(force=True)
    if not healthy:
        return jsonify({"error": f"API tidak siap: {health_msg}"}), 503

    file = request.files.get("file")
    if not file:
        return jsonify({"error": "File tidak ditemukan"}), 400

    # Baca Excel — validasi kolom wajib
    try:
        raw_bytes = file.read()
        df = pd.read_excel(io.BytesIO(raw_bytes), dtype=str)
        df.columns = [c.strip().lower() for c in df.columns]

        required = {"nama", "rekening", "bank"}
        missing  = required - set(df.columns)
        if missing:
            return jsonify({"error": f"Kolom tidak ditemukan: {', '.join(sorted(missing))}"}), 400

        records = df[["nama", "rekening", "bank"]].to_dict("records")
    except Exception as exc:
        return jsonify({"error": f"Gagal membaca Excel: {exc}"}), 400

    if not records:
        return jsonify({"error": "File kosong atau tidak ada data"}), 400

    if len(records) > q:
        return jsonify({
            "error": f"Data ({len(records)} baris) melebihi sisa kuota ({q}). Hubungi admin."
        }), 403

    return Response(
        stream_generator(records, code),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":       "keep-alive",
        },
    )


@app.route("/template")
def download_template():
    df = pd.DataFrame({
        "nama":     ["TEGUH HASYA", "BAMBANG SUGITO", "WAHYU NUR IMAN", "SITI RAHAYU"],
        "rekening": ["2840446855",  "7330699393",     "1330024362634",  "0987654321"],
        "bank":     ["bca",         "bca",            "mandiri",        "bri"],
    })
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    buf.seek(0)
    return send_file(
        buf, as_attachment=True, download_name="template.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/download", methods=["POST"])
def download():
    body   = request.get_json(silent=True) or {}
    rows   = body.get("data", [])
    fmt    = body.get("format", "xlsx").lower()

    cols = ["No", "Nama", "Rekening", "Bank", "Nama Bank", "Hasil"]
    df   = pd.DataFrame(rows, columns=cols)
    buf  = io.BytesIO()

    if fmt == "csv":
        df.to_csv(buf, index=False, sep=",", encoding="utf-8-sig")
        buf.seek(0)
        return send_file(buf, as_attachment=True, download_name="hasil_validasi.csv", mimetype="text/csv")

    df.to_excel(buf, index=False)
    buf.seek(0)
    return send_file(
        buf, as_attachment=True, download_name="hasil_validasi.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/supported-banks")
def supported_banks():
    """Download daftar bank yang didukung API sebagai Excel."""
    try:
        resp = requests.get(
            "https://use.api.co.id/validation/bank/available",
            headers={"x-api-co-id": API_KEY, "Accept": "application/json"},
            timeout=15,
        )
        if resp.status_code == 200:
            body = resp.json()
            if body.get("is_success"):
                banks = body.get("data", {}).get("banks", [])
                rows = [
                    {
                        "No":         i + 1,
                        "Nama Bank":  b.get("bank_name", "").title(),
                        "Kode API":   b.get("bank_code", ""),
                        "Input Excel": re.sub(r"^bank_", "", b.get("bank_code", "")).upper(),
                    }
                    for i, b in enumerate(banks)
                ]
                df  = pd.DataFrame(rows)
                buf = io.BytesIO()
                df.to_excel(buf, index=False)
                buf.seek(0)
                return send_file(
                    buf, as_attachment=True, download_name="daftar_bank_support.xlsx",
                    mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
    except Exception as exc:
        log.error("supported_banks: %s", exc)
    return jsonify({"error": "Gagal mengambil daftar bank"}), 500


# ─────────────────────────────────────────────────────────────────────────────
# FLASK ROUTES — Admin API
# ─────────────────────────────────────────────────────────────────────────────

def _require_admin():
    """Return True jika request punya header admin yang valid."""
    return request.headers.get("X-Admin-Secret", "") == ADMIN_PWD


@app.route("/admin/add", methods=["POST"])
def admin_add():
    if not _require_admin():
        return jsonify({"ok": False, "msg": "Unauthorized"}), 401
    body   = request.get_json(silent=True) or {}
    code   = str(body.get("code", "")).strip().upper()
    amount = max(1, int(body.get("quota", 100)))
    if not code:
        return jsonify({"ok": False, "msg": "Kode tidak boleh kosong"}), 400
    new_q = quota_add(code, amount)
    log.info("ADMIN add %s +%d → total %d", code, amount, new_q)
    return jsonify({"ok": True, "code": code, "quota": new_q})


@app.route("/admin/set", methods=["POST"])
def admin_set():
    if not _require_admin():
        return jsonify({"ok": False, "msg": "Unauthorized"}), 401
    body   = request.get_json(silent=True) or {}
    code   = str(body.get("code", "")).strip().upper()
    amount = max(0, int(body.get("quota", 0)))
    if not code:
        return jsonify({"ok": False, "msg": "Kode tidak boleh kosong"}), 400
    quota_set(code, amount)
    log.info("ADMIN set %s → %d", code, amount)
    return jsonify({"ok": True, "code": code, "quota": amount})


@app.route("/admin/delete", methods=["DELETE"])
def admin_delete():
    if not _require_admin():
        return jsonify({"ok": False, "msg": "Unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    code = str(body.get("code", "")).strip().upper()
    if not code:
        return jsonify({"ok": False, "msg": "Kode tidak boleh kosong"}), 400
    if _redis:
        _redis.delete(f"q:{code}")
    else:
        with _lock:
            d = _codes_read()
            d.pop(code, None)
            _codes_write(d)
    log.info("ADMIN delete %s", code)
    return jsonify({"ok": True, "msg": f"Kode {code} berhasil dihapus"})


@app.route("/admin/list", methods=["GET"])
def admin_list():
    if not _require_admin():
        return jsonify({"ok": False, "msg": "Unauthorized"}), 401
    codes = quota_list()
    return jsonify({
        "ok":    True,
        "codes": codes,
        "total": len(codes),
        "active": sum(1 for c in codes if c["quota"] > 0),
        "total_quota": sum(c["quota"] for c in codes),
    })


@app.route("/admin/debug", methods=["GET"])
def admin_debug():
    """Test konektivitas & format API — berguna saat trouble-shoot."""
    if not _require_admin():
        return jsonify({"ok": False, "msg": "Unauthorized"}), 401

    # Jalankan health check dulu
    healthy, health_msg = api_health_check(force=True)

    tests: dict = {
        "api_key_set":    bool(API_KEY),
        "api_key_prefix": (API_KEY[:12] + "…") if API_KEY else "—",
        "redis":          _redis is not None,
        "health_check":   {"ok": healthy, "message": health_msg},
    }

    sess = get_session()

    # Test endpoint
    for bank_code in ("bank_bca", "bca"):
        try:
            r = sess.get(
                API_ENDPOINT,
                params={"bank_code": bank_code, "account_number": "0201245750", "account_name": "TEST"},
                headers={"x-api-co-id": API_KEY, "Accept": "application/json"},
                timeout=15,
            )
            tests[f"endpoint_{bank_code}"] = {
                "status": r.status_code, "body": r.text[:300]
            }
        except Exception as exc:
            tests[f"endpoint_{bank_code}"] = {"error": str(exc)}

    return jsonify(tests)


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN PANEL UI
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/admin")
def admin_panel():
    return ADMIN_HTML


ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin — Rekening Validator Pro</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{--bg:#02040a;--s1:#0d1117;--s2:#111827;--b:rgba(0,229,255,.12);
  --c:#00e5ff;--g:#00ffa3;--r:#ff4d6d;--y:#fbbf24;--t:#e2e8f0;--m:#64748b;
  --mono:'JetBrains Mono',monospace;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--t);font-family:'Syne',sans-serif;min-height:100vh}
.hdr{background:var(--s1);border-bottom:1px solid var(--b);padding:18px 30px;
  display:flex;align-items:center;gap:16px}
.hdr h1{font-size:18px;font-weight:700;background:linear-gradient(135deg,#fff,var(--c));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent}
.pill{background:rgba(0,229,255,.08);border:1px solid var(--b);color:var(--c);
  padding:3px 12px;border-radius:100px;font-size:11px;font-family:var(--mono)}
.body{padding:28px 24px;max-width:900px;margin:0 auto}
.card{background:var(--s1);border:1px solid var(--b);border-radius:14px;padding:22px;margin-bottom:18px}
.ctitle{color:var(--c);font-size:11px;letter-spacing:2px;text-transform:uppercase;
  font-weight:700;margin-bottom:16px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
input[type=text],input[type=password],input[type=number]{
  background:var(--s2);border:1px solid var(--b);color:var(--t);
  padding:10px 13px;border-radius:8px;font-size:13px;font-family:var(--mono);
  outline:none;transition:border-color .2s}
input:focus{border-color:var(--c)}
input[type=text]{text-transform:uppercase}
.f1{flex:1;min-width:180px}
.f0{width:130px}
.btn{padding:10px 18px;border:none;border-radius:8px;font-size:12px;font-weight:600;
  cursor:pointer;transition:.2s;white-space:nowrap;font-family:inherit}
.btn-c{background:var(--c);color:#000}.btn-c:hover{opacity:.85}
.btn-g{background:rgba(0,255,163,.1);color:var(--g);border:1px solid rgba(0,255,163,.25)}
.btn-g:hover{background:rgba(0,255,163,.2)}
.btn-y{background:rgba(251,191,36,.1);color:var(--y);border:1px solid rgba(251,191,36,.25);
  padding:6px 12px;font-size:11px}
.btn-r{background:rgba(255,77,109,.1);color:var(--r);border:1px solid rgba(255,77,109,.25);
  padding:6px 12px;font-size:11px}
.msg{margin-top:11px;font-size:12px;min-height:16px;font-family:var(--mono)}
.ok{color:var(--g)}.er{color:var(--r)}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
.sbox{background:var(--s2);border:1px solid var(--b);border-radius:10px;padding:16px;text-align:center}
.snum{font-size:26px;font-weight:700;color:var(--c);font-family:var(--mono)}
.slbl{font-size:11px;color:var(--m);margin-top:4px}
table{width:100%;border-collapse:collapse;font-size:12px}
th{background:var(--s2);color:var(--c);padding:10px 13px;text-align:left;
  font-size:10px;letter-spacing:1.5px;border-bottom:1px solid var(--b)}
td{padding:10px 13px;border-bottom:1px solid rgba(30,45,69,.4);font-family:var(--mono)}
tbody tr:hover{background:rgba(0,229,255,.02)}
.chip{padding:3px 9px;border-radius:4px;font-size:10px;font-weight:700}
.chip-g{background:rgba(0,255,163,.12);color:var(--g)}
.chip-r{background:rgba(255,77,109,.12);color:var(--r)}
.tdact{display:flex;gap:6px;align-items:center}
.ein{width:80px;padding:5px 8px;font-size:12px}
.auth-ov{position:fixed;inset:0;background:var(--bg);display:flex;
  align-items:center;justify-content:center;z-index:100}
.auth-bx{background:var(--s1);border:1px solid var(--b);border-radius:18px;
  padding:40px 36px;width:400px;text-align:center}
.auth-bx h2{color:var(--c);font-size:16px;margin-bottom:8px}
.auth-bx p{color:var(--m);font-size:13px;margin-bottom:22px}
.auth-bx input{width:100%;margin-bottom:12px;font-size:15px;letter-spacing:2px;text-align:center}
.auth-bx button{width:100%}
</style>
</head>
<body>

<div class="auth-ov" id="authOv">
  <div class="auth-bx">
    <h2>🔐 Admin Panel</h2>
    <p>Masukkan Admin Secret untuk akses</p>
    <input type="password" id="authIn" placeholder="Admin Secret"
      onkeydown="if(event.key==='Enter')doAuth()">
    <button class="btn btn-c" onclick="doAuth()">Login</button>
    <div class="msg er" id="authErr"></div>
  </div>
</div>

<div id="main" style="display:none">
  <div class="hdr">
    <h1>⚡ LICENSE MANAGER</h1>
    <span class="pill" id="storePill">—</span>
    <div style="margin-left:auto;display:flex;gap:8px">
      <button class="btn btn-g" onclick="loadAll()" style="padding:7px 14px;font-size:11px">↻ Refresh</button>
      <button class="btn" onclick="runDebug()"
        style="background:rgba(124,58,237,.12);color:#a78bfa;border:1px solid rgba(124,58,237,.25);padding:7px 14px;font-size:11px">
        🔧 Debug API
      </button>
    </div>
  </div>

  <div class="body">

    <div class="card" id="statCard" style="display:none">
      <div class="ctitle">Statistik</div>
      <div class="grid3">
        <div class="sbox"><div class="snum" id="stT">0</div><div class="slbl">Total Kode</div></div>
        <div class="sbox"><div class="snum" id="stA">0</div><div class="slbl">Kode Aktif</div></div>
        <div class="sbox"><div class="snum" id="stQ">0</div><div class="slbl">Total Kuota</div></div>
      </div>
    </div>

    <div class="card">
      <div class="ctitle">➕ Tambah / Top-up Kode</div>
      <div class="row">
        <input type="text" class="f1" id="iCode" placeholder="MANZ-VIP-001">
        <input type="number" class="f0" id="iQuota" value="100" min="1" placeholder="Kuota">
        <button class="btn btn-g" onclick="addCode()">Tambah / Top-up</button>
      </div>
      <div class="msg" id="addMsg"></div>
    </div>

    <div class="card">
      <div class="ctitle">📋 Daftar Kode</div>
      <table>
        <thead><tr><th>#</th><th>Kode Lisensi</th><th>Sisa Kuota</th><th>Status</th><th>Aksi</th></tr></thead>
        <tbody id="tbl">
          <tr><td colspan="5" style="text-align:center;color:var(--m);padding:28px">Memuat...</td></tr>
        </tbody>
      </table>
    </div>

    <div class="card" id="dbgCard" style="display:none">
      <div class="ctitle">🔧 Debug API</div>
      <pre id="dbgOut" style="font-size:11px;color:#94a3b8;white-space:pre-wrap;line-height:1.7;
        background:var(--s2);padding:14px;border-radius:8px;border:1px solid var(--b)">—</pre>
    </div>

  </div>
</div>

<script>
let SEC = '';

function doAuth() {
  const s = document.getElementById('authIn').value.trim();
  if (!s) return;
  fetch('/admin/list', { headers: { 'X-Admin-Secret': s } })
  .then(r => {
    if (r.ok) {
      SEC = s;
      document.getElementById('authOv').style.display = 'none';
      document.getElementById('main').style.display   = 'block';
      loadAll();
    } else {
      document.getElementById('authErr').textContent = '✗ Secret salah';
    }
  });
}

function setMsg(id, t, ok) {
  const el = document.getElementById(id);
  el.textContent = t; el.className = 'msg ' + (ok ? 'ok' : 'er');
}

async function loadAll() {
  const r = await fetch('/admin/list', { headers: { 'X-Admin-Secret': SEC } });
  const d = await r.json();
  if (!d.ok) return;

  document.getElementById('stT').textContent = d.total;
  document.getElementById('stA').textContent = d.active;
  document.getElementById('stQ').textContent = (d.total_quota||0).toLocaleString();
  document.getElementById('statCard').style.display = '';
  document.getElementById('storePill').textContent  = d.redis ? 'Redis' : 'Local JSON';

  const tb = document.getElementById('tbl');
  if (!d.codes.length) {
    tb.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--m);padding:28px">Belum ada kode</td></tr>';
    return;
  }
  tb.innerHTML = d.codes.map((c, i) => `<tr>
    <td style="color:var(--m)">${i+1}</td>
    <td><b style="color:#fff;letter-spacing:1px">${c.code}</b></td>
    <td><b style="color:${c.quota>0?'var(--g)':'var(--r)'];font-size:15px">${c.quota.toLocaleString()}</b></td>
    <td><span class="chip ${c.quota>0?'chip-g':'chip-r'}">${c.quota>0?'AKTIF':'HABIS'}</span></td>
    <td><div class="tdact">
      <input class="ein" type="number" id="eq_${c.code}" value="${c.quota}" min="0">
      <button class="btn btn-y" onclick="setQ('${c.code}')">Set</button>
      <button class="btn btn-r" onclick="del('${c.code}')">Hapus</button>
    </div></td>
  </tr>`).join('');
}

async function addCode() {
  const code  = document.getElementById('iCode').value.trim().toUpperCase();
  const quota = parseInt(document.getElementById('iQuota').value);
  if (!code)  return setMsg('addMsg','⚠ Isi kode',false);
  if (!quota) return setMsg('addMsg','⚠ Kuota min 1',false);
  const r = await fetch('/admin/add', {
    method:'POST', headers:{'Content-Type':'application/json','X-Admin-Secret':SEC},
    body: JSON.stringify({code,quota})
  });
  const d = await r.json();
  setMsg('addMsg', d.ok ? `✓ Kode ${d.code} — Total kuota: ${d.quota}` : '✗ '+d.msg, d.ok);
  if (d.ok) { document.getElementById('iCode').value=''; loadAll(); }
}

async function setQ(code) {
  const quota = parseInt(document.getElementById('eq_'+code).value);
  const r = await fetch('/admin/set', {
    method:'POST', headers:{'Content-Type':'application/json','X-Admin-Secret':SEC},
    body: JSON.stringify({code,quota})
  });
  const d = await r.json();
  setMsg('addMsg', d.ok ? `✓ ${code} → ${quota}` : '✗ Gagal', d.ok);
  if (d.ok) loadAll();
}

async function del(code) {
  if (!confirm(`Hapus "${code}"?\nKonsumen tidak bisa login lagi.`)) return;
  const r = await fetch('/admin/delete', {
    method:'DELETE', headers:{'Content-Type':'application/json','X-Admin-Secret':SEC},
    body: JSON.stringify({code})
  });
  const d = await r.json();
  setMsg('addMsg', d.ok ? `✓ ${code} dihapus` : '✗ '+d.msg, d.ok);
  if (d.ok) loadAll();
}

async function runDebug() {
  const card = document.getElementById('dbgCard');
  const out  = document.getElementById('dbgOut');
  card.style.display = '';
  out.textContent = 'Testing...';
  const r = await fetch('/admin/debug', { headers:{'X-Admin-Secret':SEC} });
  const d = await r.json();
  out.textContent = JSON.stringify(d, null, 2);
}

document.getElementById('authIn').addEventListener('keydown', e => {
  if (e.key === 'Enter') doAuth();
});
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
