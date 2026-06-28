# ForexAI Combined Bot - v3.0
# 4 Strategies: EMA + MSS + VPA + Breakout
# 7 Pairs: EUR/USD, GBP/USD, USD/JPY, AUD/USD, USD/CAD, USD/CHF, NZD/USD
# Confirmation candles, 2 positions/strategy, 10min cooldown
import os, time, logging, math
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, request
from flask_cors import CORS
import threading

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

OANDA_API_KEY    = os.environ.get("OANDA_API_KEY", "")
OANDA_ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID", "")
PAPER_MODE       = os.environ.get("PAPER_MODE", "true").lower() == "true"
OANDA_ENV        = "practice" if PAPER_MODE else "live"

SYMBOLS = ["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD", "USD_CHF", "NZD_USD"]
JPY_PAIRS = ["USD_JPY"]

EMA_CONFIG = {
    "name": "EMA", "rsi_hard_gate": 55, "rsi_entry_max": 40,
    "bb_period": 20, "bb_std": 2.0, "bb_min_bw": 0.05,
    "min_score": 4, "min_score_confirmed": 3,
    "atr_min_mult": 0.7, "volume_bonus_mult": 1.5,
    "time_filter": True, "time_start_utc": 7, "time_end_utc": 17,
    "bear_filter": True,
}
MSS_CONFIG = {
    "name": "MSS", "swing_lookback": 10, "swing_fallback": 7, "fallback_hours": 4,
    "rsi_soft_threshold": 50, "atr_min_mult": 0.7, "volume_bonus_mult": 1.5,
    "time_filter": True, "time_start_utc": 7, "time_end_utc": 17,
    "bear_filter": True, "min_score": 4, "min_score_confirmed": 3,
}
VPA_CONFIG = {
    "name": "VPA", "volume_spike_mult": 2.0, "volume_avg_period": 20,
    "min_close_ratio": 0.6, "effort_result_ratio": 0.02,
    "min_score": 3, "min_score_confirmed": 2,
    "time_filter": False, "bear_filter": False,
}
BREAKOUT_CONFIG = {
    "name": "Breakout", "consolidation_candles": 8, "consolidation_pips": 15,
    "breakout_volume_mult": 1.5, "breakout_candle_close_ratio": 0.65,
    "min_breakout_pips": 3, "min_score": 4, "min_score_confirmed": 3,
    "time_filter": False, "bear_filter": False,
}

RISK = {
    "position_units": 5000, "stop_loss_pips": 12, "take_profit_pips": 22,
    "max_positions_per_strategy": 2, "max_total_positions": 6,
    "cooldown_minutes": 10, "daily_loss_limit_pct": 5.0,
}

STRATEGIES = ["EMA", "MSS", "VPA", "Breakout"]

bot_state = {
    "running": True, "killed": False,
    "positions": {},
    "strategy_positions": {s: [] for s in STRATEGIES},
    "closed_trades": [], "diary": [],
    "day_pnl": 0.0, "daily_start_nav": 0.0,
    "total_trades": 0, "win_count": 0,
    "strategy_stats": {s: {"trades": 0, "wins": 0, "pnl": 0.0} for s in STRATEGIES},
    "signals": {sym: {s: {} for s in STRATEGIES} for sym in SYMBOLS},
    "account_balance": 0.0, "account_equity": 0.0, "account_nav": 0.0,
    "active_cooldowns": {},
    "market_regime": {sym: "UNKNOWN" for sym in SYMBOLS},
    "market_open": False, "in_trading_window": False,
    "daily_paused": False,
    "mss_last_signal_time": {sym: None for sym in SYMBOLS},
    "pending_confirmation": {},  # key -> {signal, candle_time, direction}
    "version": "ForexCombined-3.0"
}

# ── OANDA helpers ──────────────────────────────────────────────────────
def get_oanda_client():
    import oandapyV20
    return oandapyV20.API(access_token=OANDA_API_KEY, environment=OANDA_ENV)

def get_candles(symbol, granularity="M5", count=100):
    try:
        import oandapyV20.endpoints.instruments as instruments
        client = get_oanda_client()
        params = {"granularity": granularity, "count": count, "price": "M"}
        r = instruments.InstrumentsCandles(instrument=symbol, params=params)
        client.request(r)
        result = []
        for c in r.response.get("candles", []):
            if c.get("complete", False):
                m = c["mid"]
                result.append({
                    "time": c["time"], "open": float(m["o"]), "high": float(m["h"]),
                    "low": float(m["l"]), "close": float(m["c"]),
                    "volume": int(c.get("volume", 0))
                })
        return result
    except Exception as e:
        log.error(f"Candles error {symbol}: {e}")
        return []

def pip_value(symbol):
    return 0.01 if "JPY" in symbol else 0.0001

def pips(symbol, diff):
    return abs(diff) / pip_value(symbol)

def calc_pnl(symbol, entry, exit_price, units):
    if "JPY" in symbol:
        return (exit_price - entry) * units / exit_price
    return (exit_price - entry) * units

def is_market_open():
    now = datetime.now(timezone.utc)
    wd = now.weekday()
    h = now.hour + now.minute / 60
    if wd == 4 and h >= 21: return False
    if wd == 5: return False
    if wd == 6 and h < 21: return False
    return True

def is_trading_window(cfg):
    if not cfg.get("time_filter", False): return True
    now = datetime.now(timezone.utc)
    return cfg["time_start_utc"] <= now.hour + now.minute/60 <= cfg["time_end_utc"]

def get_account_info():
    try:
        import oandapyV20.endpoints.accounts as accounts
        client = get_oanda_client()
        r = accounts.AccountSummary(OANDA_ACCOUNT_ID)
        client.request(r)
        acct = r.response["account"]
        bot_state["account_balance"] = float(acct.get("balance", 0))
        bot_state["account_nav"]     = float(acct.get("NAV", 0))
        bot_state["account_equity"]  = float(acct.get("NAV", 0))
        if bot_state["daily_start_nav"] == 0.0:
            bot_state["daily_start_nav"] = float(acct.get("NAV", 0))
    except Exception as e:
        log.error(f"Account error: {e}")

def sync_positions():
    try:
        import oandapyV20.endpoints.trades as trades
        client = get_oanda_client()
        r = trades.OpenTrades(OANDA_ACCOUNT_ID)
        client.request(r)
        synced = {}
        active = set()
        for t in r.response.get("trades", []):
            sym = t["instrument"]
            if sym not in SYMBOLS: continue
            active.add(sym)
            existing = bot_state["positions"].get(sym, {})
            synced[sym] = {
                "symbol": sym, "entry": float(t["price"]),
                "units": int(t["currentUnits"]), "trade_id": t["id"],
                "open_time": t.get("openTime", datetime.now(timezone.utc).isoformat()),
                "current_price": float(t["price"]),
                "unrealized_pnl": float(t.get("unrealizedPL", 0)),
                "strategy": existing.get("strategy", "UNKNOWN")
            }
        for strat in STRATEGIES:
            bot_state["strategy_positions"][strat] = [
                s for s in bot_state["strategy_positions"][strat] if s in active
            ]
        bot_state["positions"] = synced
    except Exception as e:
        log.error(f"Sync error: {e}")

def place_order(symbol, units, side):
    try:
        import oandapyV20.endpoints.orders as orders
        client = get_oanda_client()
        actual = units if side == "BUY" else -units
        data = {"order": {"type": "MARKET", "instrument": symbol, "units": str(actual)}}
        r = orders.OrderCreate(OANDA_ACCOUNT_ID, data=data)
        client.request(r)
        fill = r.response.get("orderFillTransaction", {})
        return float(fill.get("price", 0))
    except Exception as e:
        log.error(f"Order error {symbol}: {e}")
        return None

def close_position(symbol, trade_id):
    try:
        import oandapyV20.endpoints.trades as trades
        client = get_oanda_client()
        r = trades.TradeClose(OANDA_ACCOUNT_ID, trade_id)
        client.request(r)
        fill = r.response.get("orderFillTransaction", {})
        return float(fill.get("price", 0))
    except Exception as e:
        log.error(f"Close error {symbol}: {e}")
        return None

def add_diary(symbol, text, entry_type="info", strategy="SYSTEM"):
    label = f"[{strategy}] " if strategy != "SYSTEM" else ""
    entry = {"time": datetime.now(timezone.utc).strftime("%H:%M"),
             "symbol": symbol, "text": f"{label}{text}",
             "type": entry_type, "strategy": strategy}
    bot_state["diary"].insert(0, entry)
    if len(bot_state["diary"]) > 300:
        bot_state["diary"] = bot_state["diary"][:300]

# ── Indicators ─────────────────────────────────────────────────────────
def calc_ema(prices, period):
    if len(prices) < period: return []
    k = 2 / (period + 1)
    ema = [sum(prices[:period]) / period]
    for p in prices[period:]:
        ema.append(p * k + ema[-1] * (1 - k))
    return ema

def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0: return 100.0
    return 100 - (100 / (1 + ag/al))

def calc_bb(closes, period=20, std_dev=2.0):
    if len(closes) < period: return None, None, None
    window = closes[-period:]
    mid = sum(window) / period
    std = math.sqrt(sum((x-mid)**2 for x in window) / period)
    return mid - std_dev*std, mid, mid + std_dev*std

def calc_atr(bars, period=14):
    if len(bars) < period + 1: return 0.0
    trs = []
    for i in range(1, len(bars)):
        h = bars[i]["high"]; l = bars[i]["low"]; pc = bars[i-1]["close"]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    return sum(trs[-period:]) / period if len(trs) >= period else sum(trs)/len(trs)

def find_sr_levels(bars, lookback=50):
    if len(bars) < lookback: return [], []
    highs = [b["high"] for b in bars[-lookback:]]
    lows  = [b["low"]  for b in bars[-lookback:]]
    resistance, support = [], []
    for i in range(2, len(highs)-2):
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            resistance.append(highs[i])
    for i in range(2, len(lows)-2):
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            support.append(lows[i])
    return sorted(set(resistance), reverse=True)[:3], sorted(set(support))[:3]

def check_symbol_regime(symbol):
    try:
        candles = get_candles(symbol, "D", 210)
        if len(candles) < 200: return "UNKNOWN"
        closes = [c["close"] for c in candles]
        ema200 = calc_ema(closes, 200)
        if not ema200: return "UNKNOWN"
        return "BULL" if closes[-1] > ema200[-1] else "BEAR"
    except Exception as e:
        log.error(f"Regime error {symbol}: {e}")
        return "UNKNOWN"

def check_daily_loss():
    if bot_state["daily_start_nav"] == 0: return False
    loss_pct = (bot_state["daily_start_nav"] - bot_state["account_nav"]) / bot_state["daily_start_nav"] * 100
    if loss_pct >= RISK["daily_loss_limit_pct"]:
        if not bot_state["daily_paused"]:
            bot_state["daily_paused"] = True
            add_diary("SYSTEM", f"Daily loss limit {RISK['daily_loss_limit_pct']}% hit", "system")
        return True
    bot_state["daily_paused"] = False
    return False

# ── Confirmation system ────────────────────────────────────────────────
def check_confirmation(symbol, strategy, direction, current_bar):
    """Returns True if confirmation candle confirms the signal direction"""
    key = f"{symbol}_{strategy}_{direction}"
    pending = bot_state["pending_confirmation"].get(key)

    if not pending:
        return False

    # Check if the current (completed) bar confirms the direction
    if direction == "BUY":
        confirmed = current_bar["close"] > current_bar["open"]
    else:
        confirmed = current_bar["close"] < current_bar["open"]

    if confirmed:
        del bot_state["pending_confirmation"][key]
        return True

    # If 3 candles pass without confirmation, cancel
    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(pending["time"])).total_seconds()
    if elapsed > 900:  # 15 minutes (3 x 5min candles)
        del bot_state["pending_confirmation"][key]
    return False

def set_pending_confirmation(symbol, strategy, direction, signal):
    key = f"{symbol}_{strategy}_{direction}"
    bot_state["pending_confirmation"][key] = {
        "signal": signal, "direction": direction,
        "time": datetime.now(timezone.utc).isoformat()
    }

def can_enter(symbol, strategy):
    if bot_state["killed"]: return False
    if bot_state["daily_paused"]: return False
    if len(bot_state["positions"]) >= RISK["max_total_positions"]: return False
    if len(bot_state["strategy_positions"][strategy]) >= RISK["max_positions_per_strategy"]: return False
    if symbol in bot_state["positions"]: return False
    ck = f"{strategy}_{symbol}"
    if ck in bot_state["active_cooldowns"]:
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(
            bot_state["active_cooldowns"][ck])).total_seconds() / 60
        if elapsed < RISK["cooldown_minutes"]: return False
        del bot_state["active_cooldowns"][ck]
    return True

def record_exit(symbol, strategy, pnl, win):
    bot_state["strategy_positions"][strategy] = [
        s for s in bot_state["strategy_positions"][strategy] if s != symbol
    ]
    bot_state["day_pnl"] += pnl
    bot_state["total_trades"] += 1
    if win: bot_state["win_count"] += 1
    s = bot_state["strategy_stats"][strategy]
    s["trades"] += 1; s["pnl"] = round(s["pnl"] + pnl, 2)
    if win: s["wins"] += 1

# ── STRATEGY A: EMA ────────────────────────────────────────────────────
def run_ema(symbol, regime):
    cfg = EMA_CONFIG
    try:
        bars_5m = get_candles(symbol, "M5", 80)
        bars_1h = get_candles(symbol, "H1", 60)
        if len(bars_5m) < 30 or len(bars_1h) < 30: return {}
        closes = [b["close"] for b in bars_5m]
        closes_1h = [b["close"] for b in bars_1h]
        volumes = [b["volume"] for b in bars_5m]
        price = closes[-1]
        if all(v == 0 for v in volumes[-5:]): return {}

        ema9 = calc_ema(closes, 9); ema21 = calc_ema(closes, 21)
        ema50_1h = calc_ema(closes_1h, 50)
        rsi = calc_rsi(closes); rsi_prev = calc_rsi(closes[:-2])
        bb_low, bb_mid, bb_high = calc_bb(closes)
        atr = calc_atr(bars_5m); avg_atr = calc_atr(bars_5m[:-10]) if len(bars_5m) > 15 else atr
        if not ema9 or not ema21 or not ema50_1h or bb_mid is None: return {}

        bb_bw = ((bb_high - bb_low) / bb_mid) * 100 if bb_mid > 0 else 0
        avg_vol = sum(volumes[-20:]) / 20
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 0
        rsi_rising = rsi > rsi_prev
        atr_ok = avg_atr == 0 or atr >= avg_atr * cfg["atr_min_mult"]

        # Check confirmation candle
        confirmed = check_confirmation(symbol, "EMA", "BUY", bars_5m[-1])

        if rsi > cfg["rsi_hard_gate"]:
            sig = {"price": price, "rsi": round(rsi,1), "blocked": "RSI_HIGH",
                   "buy_score": 0, "confirmed": False, "strategy": "EMA"}
            bot_state["signals"][symbol]["EMA"] = sig
            return sig
        if not atr_ok:
            sig = {"price": price, "blocked": "ATR_LOW", "buy_score": 0, "confirmed": False, "strategy": "EMA"}
            bot_state["signals"][symbol]["EMA"] = sig
            return sig

        # EMA pullback check
        dist_ema21 = pips(symbol, abs(price - ema21[-1]))
        near_ema21 = dist_ema21 <= 8

        # S/R check
        resistance, support = find_sr_levels(bars_5m, 60)
        pv = pip_value(symbol)
        near_support = any(abs(price - lvl) <= 5 * pv for lvl in support)

        score = 0
        if price > ema50_1h[-1]: score += 1
        if ema9[-1] > ema21[-1]: score += 2
        if len(ema9) > 1 and ema9[-1] > ema21[-1] and ema9[-2] <= ema21[-2]: score += 1
        if rsi < 40 and rsi_rising: score += 2
        elif rsi < cfg["rsi_hard_gate"] and rsi_rising: score += 1
        if bb_bw >= cfg["bb_min_bw"] and price < bb_low: score += 1
        if vol_ratio >= cfg["volume_bonus_mult"]: score += 1
        if near_ema21 and ema9[-1] > ema21[-1]: score += 1
        if near_support: score += 1

        # Determine threshold based on confirmation
        min_score = cfg["min_score_confirmed"] if confirmed else cfg["min_score"]

        # Set pending confirmation if score meets confirmed threshold but not regular
        if score >= cfg["min_score_confirmed"] and score < cfg["min_score"] and not confirmed:
            set_pending_confirmation(symbol, "EMA", "BUY", {"score": score})

        sig = {
            "price": price, "rsi": round(rsi,1), "rsi_rising": rsi_rising,
            "bb_bw": round(bb_bw,4), "vol_ratio": round(vol_ratio,2),
            "buy_score": score, "confirmed": confirmed,
            "near_ema21": near_ema21, "near_support": near_support,
            "strategy": "EMA"
        }
        bot_state["signals"][symbol]["EMA"] = sig
        log.info(f"[EMA] {symbol} | price={price:.5f} RSI={round(rsi,1)} score={score} conf={confirmed}")
        return sig
    except Exception as e:
        log.error(f"[EMA] error {symbol}: {e}"); return {}

# ── STRATEGY B: MSS ────────────────────────────────────────────────────
def run_mss(symbol, regime):
    cfg = MSS_CONFIG
    try:
        bars_5m = get_candles(symbol, "M5", 80)
        bars_1h = get_candles(symbol, "H1", 30)
        if len(bars_5m) < 20 or len(bars_1h) < 15: return {}
        closes = [b["close"] for b in bars_5m]
        highs_1h = [b["high"] for b in bars_1h]; lows_1h = [b["low"] for b in bars_1h]
        highs_5m = [b["high"] for b in bars_5m]; lows_5m = [b["low"] for b in bars_5m]
        volumes = [b["volume"] for b in bars_5m]
        price = closes[-1]
        if all(v == 0 for v in volumes[-5:]): return {}

        rsi = calc_rsi(closes); rsi_prev = calc_rsi(closes[:-2])
        rsi_rising = rsi > rsi_prev
        atr = calc_atr(bars_5m); avg_atr = calc_atr(bars_5m[:-10]) if len(bars_5m) > 15 else atr
        avg_vol = sum(volumes[-20:]) / 20
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 0
        atr_ok = avg_atr == 0 or atr >= avg_atr * cfg["atr_min_mult"]

        if not atr_ok: return {"price": price, "blocked": "ATR_LOW", "buy_score": 0, "strategy": "MSS"}

        # 1H trend
        rh = highs_1h[-5:]; ph = highs_1h[-10:-5]
        rl = lows_1h[-5:]; pl = lows_1h[-10:-5]
        trend_1h = "NEUTRAL"
        if rh and ph and rl and pl:
            if max(rh) > max(ph) and min(rl) > min(pl): trend_1h = "BULL"
            elif max(rh) < max(ph) and min(rl) < min(pl): trend_1h = "BEAR"

        if trend_1h != "BULL":
            sig = {"price": price, "trend_1h": trend_1h, "buy_score": 0, "strategy": "MSS"}
            bot_state["signals"][symbol]["MSS"] = sig
            return sig

        # Lookback with fallback
        last_sig = bot_state["mss_last_signal_time"].get(symbol)
        lookback = cfg["swing_lookback"]
        if last_sig:
            hrs = (datetime.now(timezone.utc) - last_sig).total_seconds() / 3600
            if hrs > cfg["fallback_hours"]: lookback = cfg["swing_fallback"]

        recent_lows = lows_5m[-lookback:]
        mss = (len(recent_lows) >= 5 and recent_lows[-3] < recent_lows[-5] and
               recent_lows[-1] > recent_lows[-2])
        if mss: bot_state["mss_last_signal_time"][symbol] = datetime.now(timezone.utc)

        confirmed = check_confirmation(symbol, "MSS", "BUY", bars_5m[-1])

        score = 0
        if mss: score += 3
        if rsi < cfg["rsi_soft_threshold"] and rsi_rising: score += 2
        elif rsi < cfg["rsi_soft_threshold"]: score += 1
        if vol_ratio >= cfg["volume_bonus_mult"]: score += 1

        min_score = cfg["min_score_confirmed"] if confirmed else cfg["min_score"]
        if score >= cfg["min_score_confirmed"] and score < cfg["min_score"] and not confirmed:
            set_pending_confirmation(symbol, "MSS", "BUY", {"score": score})

        sig = {
            "price": price, "trend_1h": trend_1h, "mss_detected": mss,
            "rsi": round(rsi,1), "rsi_rising": rsi_rising,
            "vol_ratio": round(vol_ratio,2), "buy_score": score,
            "confirmed": confirmed, "lookback": lookback, "strategy": "MSS"
        }
        bot_state["signals"][symbol]["MSS"] = sig
        log.info(f"[MSS] {symbol} | trend={trend_1h} MSS={mss} score={score} conf={confirmed}")
        return sig
    except Exception as e:
        log.error(f"[MSS] error {symbol}: {e}"); return {}

# ── STRATEGY C: VPA ────────────────────────────────────────────────────
def run_vpa(symbol, regime):
    cfg = VPA_CONFIG
    try:
        bars = get_candles(symbol, "M5", 40)
        if len(bars) < 25: return {}
        volumes = [b["volume"] for b in bars]; closes = [b["close"] for b in bars]
        opens = [b["open"] for b in bars]; highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        if all(v == 0 for v in volumes[-5:]): return {}

        avg_vol = sum(volumes[-cfg["volume_avg_period"]:]) / cfg["volume_avg_period"]
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 0
        price = closes[-1]; bar_range = highs[-1] - lows[-1]
        if bar_range == 0: return {}
        close_ratio = (closes[-1] - lows[-1]) / bar_range
        price_move = bar_range / price if price > 0 else 0

        confirmed = check_confirmation(symbol, "VPA", "BUY", bars[-1])

        score = 0; signals_detected = []
        if vol_ratio >= cfg["volume_spike_mult"]:
            if close_ratio >= cfg["min_close_ratio"]:
                score += 2; signals_detected.append("VOL_SPIKE_BULL")
        if vol_ratio >= 2.5 and price_move < cfg["effort_result_ratio"]:
            if closes[-1] > opens[-1]:
                score += 2; signals_detected.append("ABSORPTION_BULL")
        if vol_ratio < 0.7 and closes[-1] > opens[-1] and close_ratio > 0.5:
            score += 1; signals_detected.append("NO_SUPPLY")

        ema20 = calc_ema(closes, 20)
        if ema20 and price > ema20[-1]: score += 1

        min_score = cfg["min_score_confirmed"] if confirmed else cfg["min_score"]
        if score >= cfg["min_score_confirmed"] and score < cfg["min_score"] and not confirmed:
            set_pending_confirmation(symbol, "VPA", "BUY", {"score": score})

        sig = {
            "price": price, "vol_ratio": round(vol_ratio,2),
            "close_ratio": round(close_ratio,2), "buy_score": score,
            "signals": signals_detected, "confirmed": confirmed, "strategy": "VPA"
        }
        bot_state["signals"][symbol]["VPA"] = sig
        log.info(f"[VPA] {symbol} | vol={round(vol_ratio,2)}x score={score} sigs={signals_detected} conf={confirmed}")
        return sig
    except Exception as e:
        log.error(f"[VPA] error {symbol}: {e}"); return {}

# ── STRATEGY D: BREAKOUT ───────────────────────────────────────────────
def run_breakout(symbol, regime):
    cfg = BREAKOUT_CONFIG
    try:
        bars = get_candles(symbol, "M5", 40)
        if len(bars) < 12: return {}
        closes = [b["close"] for b in bars]; highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]; volumes = [b["volume"] for b in bars]
        if all(v == 0 for v in volumes[-5:]): return {}

        price = closes[-1]; pv = pip_value(symbol)
        avg_vol = sum(volumes[-20:]) / len(volumes[-20:]) if volumes[-20:] else 1
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 0

        lookback = cfg["consolidation_candles"]
        consol = bars[-(lookback+2):-2]
        c_highs = [b["high"] for b in consol]; c_lows = [b["low"] for b in consol]
        c_range_pips = pips(symbol, max(c_highs) - min(c_lows))
        c_high = max(c_highs)
        in_consol = c_range_pips <= cfg["consolidation_pips"]

        bar_range = highs[-1] - lows[-1]
        close_ratio = (closes[-1] - lows[-1]) / bar_range if bar_range > 0 else 0
        bo_pips = pips(symbol, closes[-1] - c_high)

        prev = bars[-2] if len(bars) >= 2 else None
        prev_confirmed = False
        if prev:
            pr = prev["high"] - prev["low"]
            if pr > 0:
                pcr = (prev["close"] - prev["low"]) / pr
                prev_confirmed = prev["close"] > c_high and pcr >= 0.5

        is_breakout = (in_consol and closes[-1] > c_high and
                      bo_pips >= cfg["min_breakout_pips"] and
                      vol_ratio >= cfg["breakout_volume_mult"] and
                      close_ratio >= cfg["breakout_candle_close_ratio"] and
                      prev_confirmed)

        confirmed = check_confirmation(symbol, "Breakout", "BUY", bars[-1])
        score = 4 if is_breakout else 0
        min_score = cfg["min_score_confirmed"] if confirmed else cfg["min_score"]

        sig = {
            "price": price, "vol_ratio": round(vol_ratio,2),
            "consol_pips": round(c_range_pips,1), "in_consol": in_consol,
            "is_breakout": is_breakout, "buy_signal": is_breakout,
            "buy_score": score, "confirmed": confirmed,
            "consol_high": round(c_high,5), "strategy": "Breakout"
        }
        bot_state["signals"][symbol]["Breakout"] = sig
        log.info(f"[Breakout] {symbol} | vol={round(vol_ratio,1)}x consol={round(c_range_pips,1)}p breakout={is_breakout} conf={confirmed}")
        return sig
    except Exception as e:
        log.error(f"[Breakout] error {symbol}: {e}"); return {}

# ── EXIT / ENTRY ───────────────────────────────────────────────────────
def check_exits(symbol, now):
    pos = bot_state["positions"].get(symbol)
    if not pos: return
    entry = pos["entry"]; units = pos["units"]; strategy = pos.get("strategy", "UNKNOWN")
    trade_id = pos.get("trade_id", "")
    bars = get_candles(symbol, "M5", 3)
    if not bars: return
    price = bars[-1]["close"]
    pnl_pips = (price - entry) / pip_value(symbol)

    should_exit = False; reason = ""
    if pnl_pips >= RISK["take_profit_pips"]:
        should_exit = True; reason = f"Take profit (+{round(pnl_pips,1)}p)"
    elif pnl_pips <= -RISK["stop_loss_pips"]:
        should_exit = True; reason = f"Stop loss ({round(pnl_pips,1)}p)"
        bot_state["active_cooldowns"][f"{strategy}_{symbol}"] = now.isoformat()

    if should_exit:
        exit_price = close_position(symbol, trade_id)
        if exit_price:
            pnl = calc_pnl(symbol, entry, exit_price, units)
            win = pnl > 0
            record_exit(symbol, strategy, pnl, win)
            add_diary(symbol,
                f"{'WIN' if win else 'LOSS'} | {entry:.5f}→{exit_price:.5f} | "
                f"{round(pnl_pips,1)}p | ${round(pnl,2)} | {reason}",
                "win" if win else "loss", strategy)
            bot_state["closed_trades"].append({
                "symbol": symbol, "entry": entry, "exit": exit_price,
                "pnl": round(pnl,2), "pips": round(pnl_pips,1),
                "win": win, "strategy": strategy, "reason": reason,
                "time": now.strftime("%H:%M")
            })
            sync_positions()

def try_entry(symbol, strategy, sig, regime, now):
    if not can_enter(symbol, strategy): return

    cfg_map = {"EMA": EMA_CONFIG, "MSS": MSS_CONFIG, "VPA": VPA_CONFIG, "Breakout": BREAKOUT_CONFIG}
    cfg = cfg_map.get(strategy, {})
    regime_ok = bot_state["market_regime"].get(symbol, "UNKNOWN") in ["BULL", "UNKNOWN"]
    confirmed = sig.get("confirmed", False)

    # Strategy-specific gates
    if strategy == "EMA":
        if not regime_ok and cfg.get("bear_filter"): return
        if not is_trading_window(cfg): return
        if sig.get("blocked"): return
    elif strategy == "MSS":
        if not sig.get("mss_detected"): return
        if not regime_ok and cfg.get("bear_filter"): return
        if not is_trading_window(cfg): return
    elif strategy == "VPA":
        pass  # No regime or time filter
    elif strategy == "Breakout":
        if not sig.get("buy_signal") and not confirmed: return

    min_score = cfg.get("min_score_confirmed", 3) if confirmed else cfg.get("min_score", 4)
    if sig.get("buy_score", 0) < min_score: return

    entry_price = place_order(symbol, RISK["position_units"], "BUY")
    if entry_price:
        bot_state["positions"][symbol] = {
            "symbol": symbol, "entry": entry_price,
            "units": RISK["position_units"], "trade_id": "pending",
            "open_time": now.isoformat(),
            "current_price": entry_price, "unrealized_pnl": 0,
            "strategy": strategy
        }
        bot_state["strategy_positions"][strategy].append(symbol)
        sync_positions()
        conf_label = " ✓CONF" if confirmed else ""
        add_diary(symbol,
            f"BUY | {entry_price:.5f} | Score {sig.get('buy_score',0)}{conf_label} | "
            f"TP {round(entry_price + RISK['take_profit_pips'] * pip_value(symbol),5)} | "
            f"SL {round(entry_price - RISK['stop_loss_pips'] * pip_value(symbol),5)}",
            "trade", strategy)
        log.info(f"[{strategy}] ENTERED {symbol} at {entry_price}{conf_label}")

# ── TRADING LOOP ───────────────────────────────────────────────────────
def trading_loop():
    if not OANDA_API_KEY or not OANDA_ACCOUNT_ID:
        log.warning("No OANDA credentials — cannot start"); return

    add_diary("SYSTEM",
        "ForexAI v3.0 started | 4 Strategies | 7 Pairs | "
        "Confirmation candles | 2 pos/strategy | 10min cooldown | "
        "VPA+Breakout no bear filter | S/R zones | EMA pullback",
        "system")
    log.info("ForexAI Combined Bot v3.0 started")

    regime_check_time = None; daily_reset_date = None

    while True:
        try:
            if not is_market_open():
                bot_state["market_open"] = False; time.sleep(60); continue

            bot_state["market_open"] = True
            now = datetime.now(timezone.utc)

            today = now.date()
            if daily_reset_date != today:
                bot_state["day_pnl"] = 0.0; bot_state["daily_start_nav"] = 0.0
                bot_state["daily_paused"] = False; daily_reset_date = today

            get_account_info(); sync_positions()
            bot_state["in_trading_window"] = any(
                is_trading_window(c) for c in [EMA_CONFIG, MSS_CONFIG, VPA_CONFIG, BREAKOUT_CONFIG])

            if not regime_check_time or (now - regime_check_time).total_seconds() > 1800:
                for sym in SYMBOLS:
                    bot_state["market_regime"][sym] = check_symbol_regime(sym)
                regime_check_time = now

            if check_daily_loss(): time.sleep(60); continue

            for symbol in SYMBOLS:
                if bot_state["killed"]: break
                regime = bot_state["market_regime"].get(symbol, "UNKNOWN")

                check_exits(symbol, now)

                # Priority: Breakout > VPA > MSS > EMA
                for strat, run_fn in [("Breakout", run_breakout), ("VPA", run_vpa),
                                      ("MSS", run_mss), ("EMA", run_ema)]:
                    if len(bot_state["strategy_positions"][strat]) < RISK["max_positions_per_strategy"]:
                        sig = run_fn(symbol, regime)
                        if sig: try_entry(symbol, strat, sig, regime, now)

        except Exception as e:
            log.error(f"Loop error: {e}")
            import traceback; log.error(traceback.format_exc())
        time.sleep(60)

threading.Thread(target=trading_loop, daemon=True).start()

# ── Flask routes ───────────────────────────────────────────────────────
@app.after_request
def no_cache(r):
    r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    r.headers["Pragma"] = "no-cache"; return r

def clean_val(obj):
    if isinstance(obj, float): return 0.0 if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict): return {k: clean_val(v) for k, v in obj.items()}
    if isinstance(obj, list): return [clean_val(i) for i in obj]
    return obj

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat(),
                    "version": bot_state["version"], "market_open": bot_state["market_open"],
                    "positions": len(bot_state["positions"]), "symbols": len(SYMBOLS)})

@app.route("/status")
def status():
    get_account_info()
    wins = bot_state["win_count"]; total = bot_state["total_trades"]
    return jsonify(clean_val({
        "running": bot_state["running"], "killed": bot_state["killed"],
        "paper_mode": PAPER_MODE, "version": bot_state["version"],
        "market_open": bot_state["market_open"],
        "in_trading_window": bot_state["in_trading_window"],
        "positions": bot_state["positions"],
        "strategy_positions": bot_state["strategy_positions"],
        "closed_trades": bot_state["closed_trades"][-50:],
        "diary": bot_state["diary"][-100:],
        "day_pnl": bot_state["day_pnl"],
        "total_trades": total,
        "win_rate": round(wins/total*100) if total > 0 else 0,
        "strategy_stats": bot_state["strategy_stats"],
        "signals": bot_state["signals"],
        "account_balance": bot_state["account_balance"],
        "account_equity": bot_state["account_equity"],
        "account_nav": bot_state["account_nav"],
        "active_cooldowns": bot_state["active_cooldowns"],
        "market_regime": bot_state["market_regime"],
        "daily_paused": bot_state["daily_paused"],
        "pending_confirmations": len(bot_state["pending_confirmation"]),
    }))

@app.route("/diary")
def diary():
    sf = request.args.get("strategy")
    entries = bot_state["diary"]
    if sf: entries = [e for e in entries if e.get("strategy") == sf]
    return jsonify({"diary": entries})

@app.route("/kill", methods=["POST"])
def kill():
    bot_state["killed"] = not bot_state["killed"]
    add_diary("SYSTEM", f"Kill switch {'KILLED' if bot_state['killed'] else 'RESUMED'}", "system")
    return jsonify({"killed": bot_state["killed"]})

@app.route("/bars")
def bars():
    symbol = request.args.get("symbol", "EUR_USD")
    tf = request.args.get("timeframe", "M5")
    candles = get_candles(symbol, tf, 150)
    result = []
    for c in candles:
        try:
            t = int(datetime.fromisoformat(c["time"].replace("Z","+00:00")).timestamp())
            result.append({"time": t, "open": c["open"], "high": c["high"],
                           "low": c["low"], "close": c["close"]})
        except: pass
    return jsonify(result)

@app.route("/history")
def history():
    sf = request.args.get("strategy")
    trades = bot_state["closed_trades"]
    if sf: trades = [t for t in trades if t.get("strategy") == sf]
    return jsonify({"trades": trades})

@app.route("/stats")
def stats():
    return jsonify(clean_val({
        "overall": {"total_trades": bot_state["total_trades"],
            "win_rate": round(bot_state["win_count"]/bot_state["total_trades"]*100)
                if bot_state["total_trades"] > 0 else 0, "day_pnl": bot_state["day_pnl"]},
        "by_strategy": {s: {"trades": bot_state["strategy_stats"][s]["trades"],
            "wins": bot_state["strategy_stats"][s]["wins"],
            "win_rate": round(bot_state["strategy_stats"][s]["wins"]/bot_state["strategy_stats"][s]["trades"]*100)
                if bot_state["strategy_stats"][s]["trades"] > 0 else 0,
            "pnl": bot_state["strategy_stats"][s]["pnl"]} for s in STRATEGIES}
    }))

@app.route("/")
def index():
    try:
        with open("index.html") as f: return f.read()
    except: return jsonify({"status": "ForexAI v3.0", "symbols": SYMBOLS})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
