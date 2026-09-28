"""마틴봇 대시보드 — 실행하면 브라우저에서 http://127.0.0.1:8765 이 열립니다.

data/auth.json 에 비밀번호가 설정되어 있으면(set_password.py) 로그인해야 접속할 수 있습니다.
서버에 올릴 때는 반드시 비밀번호를 설정하세요.
"""
import json
import os
import secrets
import threading
import time
import webbrowser
from datetime import timedelta

from flask import Flask, jsonify, request, send_from_directory, session, redirect
from werkzeug.security import check_password_hash

from bot import Bot
from backtest import fetch_candles, run_backtest, run_sweep
from exchanges import exchange_class, ExchangeError

VERSION = "1.4"
BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE, "static"))
bot = Bot()
bt_state = {"running": False, "progress": "", "result": None, "error": ""}

# ---------------- 로그인 ----------------
DATA = os.path.join(BASE, "data")
AUTH_PATH = os.path.join(DATA, "auth.json")
os.makedirs(DATA, exist_ok=True)


def _secret_key():
    p = os.path.join(DATA, "secret_key")
    if not os.path.exists(p):
        with open(p, "w") as f:
            f.write(secrets.token_hex(32))
    with open(p) as f:
        return f.read().strip()


app.secret_key = _secret_key()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
                  SESSION_COOKIE_SECURE=bool(os.environ.get("MARTIN_SECURE_COOKIE")),
                  PERMANENT_SESSION_LIFETIME=timedelta(days=14))
fails = {}  # ip -> [실패 시각들]


def password_hash():
    if os.path.exists(AUTH_PATH):
        with open(AUTH_PATH, encoding="utf-8") as f:
            return json.load(f).get("hash")
    return None


def client_ip():
    if request.remote_addr == "127.0.0.1" and request.headers.get("X-Forwarded-For"):
        return request.headers["X-Forwarded-For"].split(",")[-1].strip()
    return request.remote_addr


@app.before_request
def require_login():
    # 다른 사이트에서 몰래 보내는 요청 차단
    if request.method == "POST" and request.headers.get("X-Martin") != "1":
        return jsonify({"ok": False, "errors": ["잘못된 요청입니다."]}), 403
    if request.path in ("/login", "/logout") or not password_hash():
        return None
    if not session.get("ok"):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "errors": ["로그인이 필요합니다."], "login": True}), 401
        return redirect("/login")
    return None


@app.get("/login")
def login_page():
    return send_from_directory(app.static_folder, "login.html")


@app.post("/login")
def login():
    ip, now = client_ip(), time.time()
    recent = [t for t in fails.get(ip, []) if now - t < 600]
    if len(recent) >= 5:
        return jsonify({"ok": False, "error": "비밀번호를 5번 틀려 10분간 잠겼습니다."}), 429
    pw = (request.get_json(silent=True) or {}).get("password", "")
    h = password_hash()
    if h and check_password_hash(h, pw):
        fails.pop(ip, None)
        session.clear()
        session["ok"] = True
        session.permanent = True
        return jsonify({"ok": True})
    recent.append(now)
    fails[ip] = recent
    time.sleep(1)
    return jsonify({"ok": False, "error": f"비밀번호가 틀렸습니다. ({len(recent)}/5)"}), 401


@app.post("/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.after_request
def no_cache(resp):
    # 업데이트 후 브라우저가 예전 화면을 보여주지 않도록
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/status")
def status():
    return jsonify({**bot.status(), "auth": bool(password_hash()), "version": VERSION})


@app.get("/api/config")
def get_config():
    return jsonify(bot.public_config())


@app.post("/api/config")
def set_config():
    errs = bot.update_config(request.get_json(force=True))
    return jsonify({"ok": not errs, "errors": errs, "config": bot.public_config()})


@app.post("/api/start")
def start():
    errs = bot.start()
    return jsonify({"ok": not errs, "errors": errs})


@app.post("/api/stop")
def stop():
    bot.stop()
    return jsonify({"ok": True})


@app.post("/api/reset")
def reset():
    errs = bot.reset_position((request.get_json(force=True) or {}).get("coin", ""))
    return jsonify({"ok": not errs, "errors": errs})


@app.post("/api/resume-coin")
def resume_coin():
    errs = bot.resume_coin((request.get_json(force=True) or {}).get("coin", ""))
    return jsonify({"ok": not errs, "errors": errs})


@app.post("/api/test-keys")
def test_keys():
    """API 키로 잔고 조회가 되는지 확인 (주문은 하지 않음)"""
    c = bot.cfg
    ex = exchange_class(c["exchange"])(c["access_key"], c["secret_key"])
    try:
        b = ex.balances()
        return jsonify({"ok": True, "krw": b.get("KRW", 0),
                        "coins": {x["coin"]: b.get(x["coin"], 0) for x in c["coins"]}})
    except Exception as e:
        return jsonify({"ok": False, "errors": [str(e)]})


@app.post("/api/backtest")
def backtest():
    if bt_state["running"]:
        return jsonify({"ok": False, "errors": ["백테스트가 이미 진행 중입니다."]})
    body = request.get_json(force=True) or {}
    days = int(body.get("days", 365))
    coin = body.get("coin") or bot.cfg["coins"][0]["coin"]
    if coin not in bot.slots:
        return jsonify({"ok": False, "errors": ["설정에 없는 코인입니다."]})
    cfg = bot.coin_cfg(coin)
    grid = body.get("grid")

    def job():
        bt_state.update(running=True, progress="시세 수집 중…", result=None, error="")
        try:
            ex = exchange_class(cfg["exchange"])()
            cs = fetch_candles(ex, f"KRW-{coin}", days,
                               progress=lambda n: bt_state.update(progress=f"시세 {n:,}개 수집"))
            if not cs:
                raise ExchangeError("시세 데이터를 받지 못했습니다.")
            bt_state["progress"] = "계산 중…"
            if grid:
                r = {"kind": "sweep", **run_sweep(cs, cfg, grid)}
            else:
                r = {"kind": "single", **run_backtest(cs, cfg)}
            r["coin"] = coin
            r["config"] = {k: v for k, v in cfg.items() if k not in ("access_key", "secret_key", "telegram_token", "coins")}
            bt_state["result"] = r
            bt_state["progress"] = "완료"
        except Exception as e:
            bt_state["error"] = str(e)
            bt_state["progress"] = "실패"
        finally:
            bt_state["running"] = False

    threading.Thread(target=job, daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/backtest")
def backtest_status():
    return jsonify(bt_state)


if __name__ == "__main__":
    port = int(os.environ.get("MARTIN_PORT", 8765))
    if not os.environ.get("MARTIN_NO_BROWSER"):
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    print(f"\n  마틴봇 v{VERSION}\n  폴더: {BASE}\n  대시보드: http://127.0.0.1:{port}\n  이 창을 닫으면 봇도 종료됩니다.\n")
    bot.resume_if_needed()  # 서버 재부팅 등으로 꺼졌다 켜지면 이어서 실행
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False, threaded=True)
