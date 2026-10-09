"""Martin Bot 대시보드 — 실행하면 브라우저에서 http://127.0.0.1:8765 이 열립니다.

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
from backtest import fetch_candles, run_backtest, run_sweep, run_optimize
from exchanges import exchange_class, ExchangeError, my_public_ip
from grid_bot import GridBot
from grid_backtest import run_grid_backtest, run_grid_sweep, run_gap_sweep, suggest_range
from grid_strategy import validate as grid_validate, apply_range_mode

VERSION = "1.9"
BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE, "static"))
bot = Bot()
grid = GridBot(bot)
bot.extra_validate = lambda martin_cfg: grid.check_conflict(martin_cfg=martin_cfg)
bt_state = {"running": False, "progress": "", "result": None, "error": ""}
gbt_state = {"running": False, "progress": "", "result": None, "error": ""}

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
    """API 키로 잔고 조회가 되는지 확인 (주문은 하지 않음). 화면에 입력한 값이 있으면 저장 전이라도 그 값으로 확인"""
    c = bot.cfg
    body = request.get_json(silent=True) or {}

    def pick(k):
        v = (body.get(k) or "").strip()
        return c[k] if (not v or "•" in v) else v

    exch = body.get("exchange") or c["exchange"]
    ex = exchange_class(exch)(pick("access_key"), pick("secret_key"))
    try:
        b = ex.balances()
        return jsonify({"ok": True, "krw": b.get("KRW", 0), "ip": my_public_ip(),
                        "coins": {x["coin"]: b.get(x["coin"], 0) for x in c["coins"]}})
    except Exception as e:
        return jsonify({"ok": False, "errors": [str(e)], "ip": my_public_ip()})


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


@app.post("/api/optimize")
def optimize():
    if bt_state["running"]:
        return jsonify({"ok": False, "errors": ["백테스트가 이미 진행 중입니다."]})
    body = request.get_json(force=True) or {}
    try:
        capital = int(float(body.get("capital", 600000)))
        split = int(body.get("split", 1))
        days = int(body.get("days", 730))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "errors": ["입력값이 올바르지 않습니다."]})
    coins = [c for c in (body.get("coins") or ["BTC", "ETH", "XRP", "SOL"]) if isinstance(c, str)][:6]
    if capital < 30000 or not (1 <= split <= len(coins)):
        return jsonify({"ok": False, "errors": ["자본은 3만 원 이상, 나눌 코인 수는 1~코인 개수 사이여야 합니다."]})
    base = {**bot.cfg, **(bot.cfg["coins"][0] if bot.cfg["coins"] else {})}

    def job():
        bt_state.update(running=True, progress="시세 수집 중…", result=None, error="")
        try:
            ex = exchange_class(base["exchange"])()
            data = {}
            for c in coins:
                data[c] = fetch_candles(ex, f"KRW-{c}", days,
                                        progress=lambda n, c=c: bt_state.update(progress=f"{c} 시세 {n:,}개 수집"))
            data = {c: v for c, v in data.items() if len(v) > 200}
            if not data:
                raise ExchangeError("시세 데이터를 받지 못했습니다.")
            r = run_optimize(data, base, capital, min(split, len(data)),
                             progress=lambda msg: bt_state.update(progress=msg))
            bt_state["result"] = {"kind": "optimize", "days": days, **r}
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


# ---------------- 전체 (Martin + Grid) ----------------
@app.post("/api/all/start")
def all_start():
    """멈춰 있는 봇만 시작. 한쪽이 설정 문제로 못 켜져도 다른 쪽은 켭니다."""
    m = [] if bot.running else bot.start()
    g = [] if (grid.running or grid.stopping) else grid.start()
    errs = [f"Martin: {e}" for e in m] + [f"Grid: {e}" for e in g]
    return jsonify({"ok": not errs, "errors": errs, "martin": bot.running, "grid": grid.running})


@app.post("/api/all/stop")
def all_stop():
    if bot.running:
        bot.stop()
    if grid.running:
        grid.stop()
    return jsonify({"ok": True})


# ---------------- 그리드 ----------------
@app.get("/api/grid/status")
def grid_status():
    return jsonify(grid.status())


@app.get("/api/grid/config")
def grid_get_config():
    return jsonify(grid.public_config())


@app.post("/api/grid/config")
def grid_set_config():
    errs = grid.update_config(request.get_json(force=True) or {})
    return jsonify({"ok": not errs, "errors": errs, "config": grid.public_config()})


@app.post("/api/grid/start")
def grid_start():
    errs = grid.start()
    return jsonify({"ok": not errs, "errors": errs})


@app.post("/api/grid/stop")
def grid_stop():
    errs = grid.stop()
    return jsonify({"ok": not errs, "errors": errs})


@app.post("/api/grid/clear")
def grid_clear():
    body = request.get_json(force=True) or {}
    errs = grid.clear(body.get("coin", ""), body.get("action", ""))
    return jsonify({"ok": not errs, "errors": errs})


@app.post("/api/grid/suggest")
def grid_suggest():
    """최근 N일 고가·저가로 범위 제안"""
    body = request.get_json(force=True) or {}
    days = max(7, min(365, int(body.get("days", 30))))
    exch = body.get("exchange") or grid.cfg["exchange"]
    coin = (body.get("coin") or grid.cfg["coins"][0]["coin"]).upper()
    try:
        ex = exchange_class(exch)()
        cs = fetch_candles(ex, f"KRW-{coin}", days)
        r = suggest_range(cs, days)
        if not r:
            raise ExchangeError("시세 데이터를 받지 못했습니다.")
        try:
            r["tick"] = ex.market_info(f"KRW-{coin}")["tick"]
        except Exception:
            r["tick"] = 0
        return jsonify({"ok": True, **r})
    except Exception as e:
        return jsonify({"ok": False, "errors": [str(e)]})


@app.post("/api/grid/backtest")
def grid_backtest():
    if gbt_state["running"]:
        return jsonify({"ok": False, "errors": ["그리드 백테스트가 이미 진행 중입니다."]})
    body = request.get_json(force=True) or {}
    days = int(body.get("days", 180))
    cfg = grid.coin_cfg((body.get("coin") or grid.cfg["coins"][0]["coin"]).upper())
    for k in ("lower", "upper", "fee_pct", "gap_pct"):
        if body.get(k) not in (None, ""):
            cfg[k] = float(body[k])
    for k in ("grids", "krw_per_grid", "max_buy_orders"):
        if body.get(k) not in (None, ""):
            cfg[k] = int(float(body[k]))
    if body.get("spacing") in ("geom", "arith"):
        cfg["spacing"] = body["spacing"]
    if body.get("range_mode") in ("gap", "range"):
        cfg["range_mode"] = body["range_mode"]
    apply_range_mode(cfg)
    if body.get("coin"):
        cfg["coin"] = str(body["coin"]).upper()
    if body.get("exchange"):
        cfg["exchange"] = body["exchange"]
    errs = [e for e in grid_validate({**cfg, "mode": "paper"}) if "손절" not in e]
    if errs:
        return jsonify({"ok": False, "errors": errs})
    counts = body.get("counts")

    def job():
        gbt_state.update(running=True, progress="시세 수집 중…", result=None, error="")
        try:
            ex = exchange_class(cfg["exchange"])()
            tick, min_krw = 0.0, 5000.0
            try:
                mi = ex.market_info(f"KRW-{cfg['coin']}")
                tick, min_krw = mi["tick"], mi["min_krw"]
            except Exception:
                pass
            cs = fetch_candles(ex, f"KRW-{cfg['coin']}", days,
                               progress=lambda n: gbt_state.update(progress=f"시세 {n:,}개 수집"))
            if not cs:
                raise ExchangeError("시세 데이터를 받지 못했습니다.")
            gbt_state["progress"] = "계산 중…"
            if counts and cfg.get("range_mode") == "gap":
                r = {"kind": "sweep", "by": "gap", "rows": run_gap_sweep(cs, cfg, counts, tick, min_krw)}
            elif counts:
                r = {"kind": "sweep", "by": "count", "rows": run_grid_sweep(cs, cfg, counts, tick, min_krw)}
            else:
                r = {"kind": "single", **run_grid_backtest(cs, cfg, tick)}
            r.update(coin=cfg["coin"], days=days)
            gbt_state["result"] = r
            gbt_state["progress"] = "완료"
        except Exception as e:
            gbt_state["error"] = str(e)
            gbt_state["progress"] = "실패"
        finally:
            gbt_state["running"] = False

    threading.Thread(target=job, daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/grid/backtest")
def grid_backtest_status():
    return jsonify(gbt_state)


if __name__ == "__main__":
    port = int(os.environ.get("MARTIN_PORT", 8765))
    if not os.environ.get("MARTIN_NO_BROWSER"):
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    print(f"\n  Martin Bot v{VERSION}\n  폴더: {BASE}\n  대시보드: http://127.0.0.1:{port}\n  이 창을 닫으면 봇도 종료됩니다.\n")
    bot.resume_if_needed()  # 서버 재부팅 등으로 꺼졌다 켜지면 이어서 실행
    grid.resume_if_needed()
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False, threaded=True)
