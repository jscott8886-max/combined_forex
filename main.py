# ForexAI Combined Bot - v7.0
# 4 Strategies: EMA + MSS + VPA + Breakout | LONG + SHORT
# 7 Pairs | ADX trend filter | Short selling enabled
# No time exit — TP/SL natural exits
import os, time, logging, math
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, request
from flask_cors import CORS
import threading
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

OANDA_API_KEY    = os.environ.get("OANDA_API_KEY", "")
OANDA_ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID", "")
PAPER_MODE       = os.environ.get("PAPER_MODE", "true").lower() == "true"
OANDA_ENV        = "practice" if PAPER_MODE else "live"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


SYMBOLS = ["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD", "USD_CHF", "NZD_USD"]
STRATEGIES = ["EMA", "MSS", "VPA", "Breakout", "Sweep"]

EMA_CONFIG = {
    "name": "EMA", "rsi_hard_gate": 55, "bb_min_bw": 0.05,
    "min_score": 4, "min_score_confirmed": 3,
    "atr_min_mult": 0.7, "volume_bonus_mult": 1.5,
    "adx_min": 20,            # shorts still use this floor
    "adx_min_long": 25,       # FIX 2: longs need a stronger trend (ADX 20-25 dead zone went 1W/5L)
    "block_long_in_bear": True,  # FIX 1: EMA longs in BEAR regime lost $54 this week — shorts only
    "time_filter": True, "time_start_utc": 7, "time_end_utc": 17,
    "blocked_pairs": [],
}
MSS_CONFIG = {
    "name": "MSS", "swing_lookback": 10, "swing_fallback": 7, "fallback_hours": 4,
    "rsi_soft_threshold": 50, "atr_min_mult": 0.7, "volume_bonus_mult": 1.5,
    "adx_min": 20,
    "time_filter": True, "time_start_utc": 12, "time_end_utc": 17,
    "blocked_pairs": ["AUD_USD"],
}
VPA_CONFIG = {
    "name": "VPA", "volume_spike_mult": 2.0, "volume_avg_period": 20,
    "min_close_ratio": 0.6, "effort_result_ratio": 0.02,
    "min_score": 3, "min_score_confirmed": 3, "bear_score_cap": 4,
    "adx_min": 18,
    "time_filter": False, "blocked_pairs": [],
}
BREAKOUT_CONFIG = {
    "name": "Breakout", "consolidation_candles": 8, "consolidation_pips": 15,
    "breakout_volume_mult": 2.0, "breakout_candle_close_ratio": 0.65,
    "min_breakout_pips": 3, "min_score": 4, "min_score_confirmed": 3,
    "adx_min": 22,
    "time_filter": True, "time_start_utc": 7, "time_end_utc": 17,
    "blocked_pairs": ["USD_CAD"],
}
SWEEP_CONFIG = {
    "name": "Sweep",
    "swing_lookback": 20,        # bars to find the swing high/low being swept
    "min_sweep_pips": 2,         # price must poke at least this far past the level
    "max_sweep_pips": 15,        # but not run away — a sweep, not a trend break
    "volume_mult": 1.5,          # sweep candle should have elevated volume (stop run)
    "min_score": 4,
    "time_filter": True, "time_start_utc": 7, "time_end_utc": 17,
    "blocked_pairs": [],
    # NOTE: Sweep has NO adx_min — it deliberately trades REVERSALS, not trends.
    # A liquidity sweep is a failed breakout; high ADX would filter out the best setups.
}

RISK = {
    "position_units": 5000, "stop_loss_pips": 12, "take_profit_pips": 22,
    "max_positions_per_strategy": 2, "max_total_positions": 6,
    "cooldown_minutes": 10, "mss_cooldown_minutes": 120,
    "counter_trend_units": 2500,  # FIX: half size when trading against trend
    "daily_loss_limit_pct": 5.0,
}

bot_state = {
    "running": True, "killed": False,
    "positions": {}, "strategy_positions": {s: [] for s in STRATEGIES},
    "closed_trades": [], "diary": [],
    "day_pnl": 0.0, "daily_start_nav": 0.0,
    "total_trades": 0, "win_count": 0,
    "strategy_stats": {s: {"trades": 0, "wins": 0, "pnl": 0.0} for s in STRATEGIES},
    "long_stats": {"trades": 0, "wins": 0, "pnl": 0.0},
    "short_stats": {"trades": 0, "wins": 0, "pnl": 0.0},
    "signals": {sym: {s: {} for s in STRATEGIES} for sym in SYMBOLS},
    "account_balance": 0.0, "account_equity": 0.0, "account_nav": 0.0,
    "active_cooldowns": {}, "market_regime": {sym: "UNKNOWN" for sym in SYMBOLS},
    "market_open": False, "in_trading_window": False,
    "daily_paused": False, "pending_confirmation": {},
    "mss_last_signal_time": {sym: None for sym in SYMBOLS},
    "loss_streak": 0,
    "version": "ForexCombined-8.2"
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
                result.append({"time": c["time"], "open": float(m["o"]), "high": float(m["h"]),
                    "low": float(m["l"]), "close": float(m["c"]), "volume": int(c.get("volume", 0))})
        return result
    except Exception as e:
        log.error(f"Candles error {symbol}: {e}"); return []

def pip_value(symbol): return 0.01 if "JPY" in symbol else 0.0001
def pips(symbol, diff): return abs(diff) / pip_value(symbol)
def calc_pnl(symbol, entry, exit_price, units):
    raw = (exit_price - entry) * units
    if "JPY" in symbol: return raw / exit_price
    return raw

def is_market_open():
    now = datetime.now(timezone.utc)
    wd = now.weekday(); h = now.hour + now.minute/60
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
        r = accounts.AccountSummary(OANDA_ACCOUNT_ID); client.request(r)
        acct = r.response["account"]
        bot_state["account_balance"] = float(acct.get("balance", 0))
        bot_state["account_nav"] = float(acct.get("NAV", 0))
        bot_state["account_equity"] = float(acct.get("NAV", 0))
        if bot_state["daily_start_nav"] == 0.0:
            bot_state["daily_start_nav"] = float(acct.get("NAV", 0))
    except Exception as e: log.error(f"Account error: {e}")

def sync_positions():
    try:
        import oandapyV20.endpoints.trades as trades
        client = get_oanda_client()
        r = trades.OpenTrades(OANDA_ACCOUNT_ID); client.request(r)
        synced = {}; active = set()
        for t in r.response.get("trades", []):
            sym = t["instrument"]
            if sym not in SYMBOLS: continue
            active.add(sym); existing = bot_state["positions"].get(sym, {})
            units = int(t["currentUnits"])
            synced[sym] = {"symbol": sym, "entry": float(t["price"]),
                "units": abs(units), "side": "short" if units < 0 else "long",
                "trade_id": t["id"],
                "open_time": existing.get("open_time", datetime.now(timezone.utc).isoformat()),
                "current_price": float(t["price"]),
                "unrealized_pnl": float(t.get("unrealizedPL", 0)),
                "strategy": existing.get("strategy", "UNKNOWN")}
        for strat in STRATEGIES:
            bot_state["strategy_positions"][strat] = [s for s in bot_state["strategy_positions"][strat] if s in active]
        bot_state["positions"] = synced
    except Exception as e: log.error(f"Sync error: {e}")

def place_order(symbol, units, side, tp_price=None, sl_price=None):
    """Place market order with server-side SL/TP — OANDA executes stops instantly"""
    try:
        import oandapyV20.endpoints.orders as orders
        client = get_oanda_client()
        actual = units if side == "BUY" else -units
        # Determine pip precision
        precision = 3 if "JPY" in symbol else 5
        order_data = {"type": "MARKET", "instrument": symbol, "units": str(actual)}
        # Server-side SL/TP — no more 60-second delay
        if sl_price:
            order_data["stopLossOnFill"] = {"price": str(round(sl_price, precision))}
        if tp_price:
            order_data["takeProfitOnFill"] = {"price": str(round(tp_price, precision))}
        data = {"order": order_data}
        r = orders.OrderCreate(OANDA_ACCOUNT_ID, data=data); client.request(r)
        fill = r.response.get("orderFillTransaction", {})
        return float(fill.get("price", 0))
    except Exception as e: log.error(f"Order error {symbol}: {e}"); return None

def close_position(symbol, trade_id):
    try:
        import oandapyV20.endpoints.trades as trades
        client = get_oanda_client()
        r = trades.TradeClose(OANDA_ACCOUNT_ID, trade_id); client.request(r)
        return float(r.response.get("orderFillTransaction", {}).get("price", 0))
    except Exception as e: log.error(f"Close error {symbol}: {e}"); return None

def add_diary(symbol, text, entry_type="info", strategy="SYSTEM"):
    label = f"[{strategy}] " if strategy != "SYSTEM" else ""
    entry = {"time": datetime.now(timezone.utc).strftime("%H:%M"), "symbol": symbol,
             "text": f"{label}{text}", "type": entry_type, "strategy": strategy}
    bot_state["diary"].insert(0, entry)
    if len(bot_state["diary"]) > 300: bot_state["diary"] = bot_state["diary"][:300]


def send_telegram(message):
    """Send notification to Telegram"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=5)
    except Exception as e:
        log.error(f"Telegram error: {e}")

# ── Indicators ─────────────────────────────────────────────────────────
def calc_ema(prices, period):
    if len(prices) < period: return []
    k = 2 / (period + 1); ema = [sum(prices[:period]) / period]
    for p in prices[period:]: ema.append(p * k + ema[-1] * (1 - k))
    return ema

def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]; gains.append(max(d, 0)); losses.append(max(-d, 0))
    ag = sum(gains[-period:]) / period; al = sum(losses[-period:]) / period
    if al == 0: return 100.0
    return 100 - (100 / (1 + ag/al))

def calc_bb(closes, period=20, std_dev=2.0):
    if len(closes) < period: return None, None, None
    window = closes[-period:]; mid = sum(window) / period
    std = math.sqrt(sum((x-mid)**2 for x in window) / period)
    return mid - std_dev*std, mid, mid + std_dev*std

def calc_atr(bars, period=14):
    if len(bars) < period + 1: return 0.0
    trs = []
    for i in range(1, len(bars)):
        h = bars[i]["high"]; l = bars[i]["low"]; pc = bars[i-1]["close"]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    return sum(trs[-period:]) / period if len(trs) >= period else sum(trs)/len(trs)

def calc_adx(bars, period=14):
    """Average Directional Index — measures trend strength regardless of direction.
    ADX > 25 = trending, ADX < 20 = choppy/ranging"""
    if len(bars) < period * 2 + 1: return 0.0
    try:
        plus_dm, minus_dm, tr_list = [], [], []
        for i in range(1, len(bars)):
            h = bars[i]["high"]; l = bars[i]["low"]
            ph = bars[i-1]["high"]; pl = bars[i-1]["low"]; pc = bars[i-1]["close"]
            tr_list.append(max(h-l, abs(h-pc), abs(l-pc)))
            up = h - ph; down = pl - l
            plus_dm.append(up if up > down and up > 0 else 0)
            minus_dm.append(down if down > up and down > 0 else 0)
        if len(tr_list) < period: return 0.0
        atr = sum(tr_list[:period]) / period
        plus_di_sum = sum(plus_dm[:period]) / period
        minus_di_sum = sum(minus_dm[:period]) / period
        for i in range(period, len(tr_list)):
            atr = (atr * (period-1) + tr_list[i]) / period
            plus_di_sum = (plus_di_sum * (period-1) + plus_dm[i]) / period
            minus_di_sum = (minus_di_sum * (period-1) + minus_dm[i]) / period
        if atr == 0: return 0.0
        plus_di = (plus_di_sum / atr) * 100
        minus_di = (minus_di_sum / atr) * 100
        di_sum = plus_di + minus_di
        if di_sum == 0: return 0.0
        dx = abs(plus_di - minus_di) / di_sum * 100
        return dx
    except: return 0.0

def check_symbol_regime(symbol):
    try:
        candles = get_candles(symbol, "D", 210)
        if len(candles) < 200: return "UNKNOWN"
        closes = [c["close"] for c in candles]; ema200 = calc_ema(closes, 200)
        if not ema200: return "UNKNOWN"
        return "BULL" if closes[-1] > ema200[-1] else "BEAR"
    except Exception as e: log.error(f"Regime error {symbol}: {e}"); return "UNKNOWN"

def can_enter(symbol, strategy, side="BUY"):
    if bot_state["killed"] or bot_state["daily_paused"]: return False
    if len(bot_state["positions"]) >= RISK["max_total_positions"]: return False
    if len(bot_state["strategy_positions"][strategy]) >= RISK["max_positions_per_strategy"]: return False
    if symbol in bot_state["positions"]: return False
    cfg_map = {"EMA": EMA_CONFIG, "MSS": MSS_CONFIG, "VPA": VPA_CONFIG, "Breakout": BREAKOUT_CONFIG, "Sweep": SWEEP_CONFIG}
    cfg = cfg_map.get(strategy, {})
    if symbol in cfg.get("blocked_pairs", []): return False
    ck = f"{strategy}_{symbol}"
    if ck in bot_state["active_cooldowns"]:
        cooldown = RISK["mss_cooldown_minutes"] if strategy == "MSS" else RISK["cooldown_minutes"]
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(bot_state["active_cooldowns"][ck])).total_seconds() / 60
        if elapsed < cooldown: return False
        del bot_state["active_cooldowns"][ck]
    return True

def record_exit(symbol, strategy, side, pnl, win):
    if strategy in bot_state["strategy_positions"]:
        bot_state["strategy_positions"][strategy] = [s for s in bot_state["strategy_positions"][strategy] if s != symbol]
    bot_state["day_pnl"] += pnl; bot_state["total_trades"] += 1
    if win:
        bot_state["win_count"] += 1
        bot_state["loss_streak"] = 0
        side_label = "LONG" if side == "long" else "SHORT"
        send_telegram(f"✅ <b>Forex WIN</b> [{strategy}] {side_label} {symbol.replace('_','/')}\n"
                      f"P&L: +${abs(pnl):.2f} | {bot_state['total_trades']} trades")
    else:
        bot_state["loss_streak"] += 1
        if bot_state["loss_streak"] >= 2:
            send_telegram(f"⚠️ <b>Forex losing streak:</b> {bot_state['loss_streak']} in a row\n"
                          f"Latest: {symbol.replace('_','/')} ${pnl:.2f}")
    if strategy in bot_state["strategy_stats"]:
        s = bot_state["strategy_stats"][strategy]
        s["trades"] += 1; s["pnl"] = round(s["pnl"] + pnl, 2)
        if win: s["wins"] += 1
    side_key = "long_stats" if side == "long" else "short_stats"
    s = bot_state[side_key]
    s["trades"] += 1; s["pnl"] = round(s["pnl"] + pnl, 2)
    if win: s["wins"] += 1

# ── STRATEGY A: EMA (LONG + SHORT) ────────────────────────────────────
def run_ema(symbol, regime, tf="M5"):
    cfg = EMA_CONFIG
    try:
        bars = get_candles(symbol, tf, 80)
        bars_1h = get_candles(symbol, "H1", 60)
        if len(bars) < 30 or len(bars_1h) < 30: return {}
        closes = [b["close"] for b in bars]; closes_1h = [b["close"] for b in bars_1h]
        volumes = [b["volume"] for b in bars]; price = closes[-1]
        if all(v == 0 for v in volumes[-5:]): return {}

        ema9 = calc_ema(closes, 9); ema21 = calc_ema(closes, 21)
        ema50_1h = calc_ema(closes_1h, 50)
        rsi = calc_rsi(closes); rsi_prev = calc_rsi(closes[:-2])
        rsi_rising = rsi > rsi_prev; rsi_falling = rsi < rsi_prev
        bb_low, bb_mid, bb_high = calc_bb(closes)
        atr = calc_atr(bars); avg_atr = calc_atr(bars[:-10]) if len(bars) > 15 else atr
        adx = calc_adx(bars)
        if not ema9 or not ema21 or not ema50_1h or bb_mid is None: return {}

        bb_bw = ((bb_high - bb_low) / bb_mid) * 100 if bb_mid > 0 else 0
        avg_vol = sum(volumes[-20:]) / 20; vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 0
        atr_ok = avg_atr == 0 or atr >= avg_atr * cfg["atr_min_mult"]
        trending = adx >= cfg["adx_min"]
        # FIX 2: longs require a stronger trend than shorts (higher ADX floor)
        trending_long = adx >= cfg.get("adx_min_long", cfg["adx_min"])
        # FIX 1: block EMA longs entirely when this pair is in a BEAR regime
        long_allowed = not (cfg.get("block_long_in_bear", False) and regime == "BEAR")

        # LONG score
        long_score = 0
        if long_allowed and rsi <= cfg["rsi_hard_gate"] and atr_ok and trending_long:
            if price > ema50_1h[-1]: long_score += 1
            if ema9[-1] > ema21[-1]: long_score += 2
            if len(ema9) > 1 and ema9[-1] > ema21[-1] and ema9[-2] <= ema21[-2]: long_score += 1
            if rsi < 40 and rsi_rising: long_score += 2
            elif rsi < cfg["rsi_hard_gate"] and rsi_rising: long_score += 1
            if bb_bw >= cfg["bb_min_bw"] and price < bb_low: long_score += 1
            if vol_ratio >= cfg["volume_bonus_mult"]: long_score += 1

        # SHORT score — mirror of long
        short_score = 0
        if rsi >= (100 - cfg["rsi_hard_gate"]) and atr_ok and trending:
            if price < ema50_1h[-1]: short_score += 1
            if ema9[-1] < ema21[-1]: short_score += 2
            if len(ema9) > 1 and ema9[-1] < ema21[-1] and ema9[-2] >= ema21[-2]: short_score += 1
            if rsi > 60 and rsi_falling: short_score += 2
            elif rsi > (100 - cfg["rsi_hard_gate"]) and rsi_falling: short_score += 1
            if bb_bw >= cfg["bb_min_bw"] and price > bb_high: short_score += 1
            if vol_ratio >= cfg["volume_bonus_mult"]: short_score += 1

        sig = {"price": price, "rsi": round(rsi,1), "adx": round(adx,1),
               "vol_ratio": round(vol_ratio,2), "trending": trending,
               "long_score": long_score, "short_score": short_score, "strategy": "EMA"}
        bot_state["signals"][symbol]["EMA" if tf=="M5" else "EMA_15m"] = sig
        log.info(f"[EMA] {symbol} | price={price:.5f} RSI={round(rsi,1)} ADX={round(adx,1)} L={long_score} S={short_score}")
        return sig
    except Exception as e: log.error(f"[EMA] error {symbol}: {e}"); return {}

# ── STRATEGY B: MSS (LONG + SHORT) ────────────────────────────────────
def run_mss(symbol, regime, tf="M5"):
    cfg = MSS_CONFIG
    try:
        bars = get_candles(symbol, tf, 80)
        bars_1h = get_candles(symbol, "H1", 30)
        if len(bars) < 20 or len(bars_1h) < 15: return {}
        closes = [b["close"] for b in bars]
        highs_1h = [b["high"] for b in bars_1h]; lows_1h = [b["low"] for b in bars_1h]
        highs = [b["high"] for b in bars]; lows = [b["low"] for b in bars]
        volumes = [b["volume"] for b in bars]; price = closes[-1]
        if all(v == 0 for v in volumes[-5:]): return {}

        rsi = calc_rsi(closes); rsi_prev = calc_rsi(closes[:-2])
        rsi_rising = rsi > rsi_prev; rsi_falling = rsi < rsi_prev
        atr = calc_atr(bars); avg_atr = calc_atr(bars[:-10]) if len(bars) > 15 else atr
        avg_vol = sum(volumes[-20:]) / 20; vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 0
        atr_ok = avg_atr == 0 or atr >= avg_atr * cfg["atr_min_mult"]
        adx = calc_adx(bars); trending = adx >= cfg["adx_min"]

        rh = highs_1h[-5:]; ph = highs_1h[-10:-5]; rl = lows_1h[-5:]; pl = lows_1h[-10:-5]
        trend_1h = "NEUTRAL"
        if rh and ph and rl and pl:
            if max(rh) > max(ph) and min(rl) > min(pl): trend_1h = "BULL"
            elif max(rh) < max(ph) and min(rl) < min(pl): trend_1h = "BEAR"

        lookback = cfg["swing_lookback"]
        last_sig = bot_state["mss_last_signal_time"].get(symbol)
        if last_sig:
            hrs = (datetime.now(timezone.utc) - last_sig).total_seconds() / 3600
            if hrs > cfg["fallback_hours"]: lookback = cfg["swing_fallback"]

        recent_lows = lows[-lookback:]; recent_highs = highs[-lookback:]
        bull_mss = len(recent_lows) >= 5 and recent_lows[-3] < recent_lows[-5] and recent_lows[-1] > recent_lows[-2]
        bear_mss = len(recent_highs) >= 5 and recent_highs[-3] > recent_highs[-5] and recent_highs[-1] < recent_highs[-2]

        if bull_mss or bear_mss:
            bot_state["mss_last_signal_time"][symbol] = datetime.now(timezone.utc)

        long_score = 0
        if bull_mss and trend_1h == "BULL" and trending:
            long_score += 3
            if rsi < cfg["rsi_soft_threshold"] and rsi_rising: long_score += 2
            if vol_ratio >= cfg["volume_bonus_mult"]: long_score += 1

        short_score = 0
        if bear_mss and trend_1h == "BEAR" and trending:
            short_score += 3
            if rsi > (100 - cfg["rsi_soft_threshold"]) and rsi_falling: short_score += 2
            if vol_ratio >= cfg["volume_bonus_mult"]: short_score += 1

        sig = {"price": price, "trend_1h": trend_1h, "adx": round(adx,1), "trending": trending,
               "bull_mss": bull_mss, "bear_mss": bear_mss, "rsi": round(rsi,1),
               "long_score": long_score, "short_score": short_score, "strategy": "MSS"}
        bot_state["signals"][symbol]["MSS" if tf=="M5" else "MSS_15m"] = sig
        log.info(f"[MSS] {symbol} | trend={trend_1h} ADX={round(adx,1)} bullMSS={bull_mss} bearMSS={bear_mss} L={long_score} S={short_score}")
        return sig
    except Exception as e: log.error(f"[MSS] error {symbol}: {e}"); return {}

# ── STRATEGY C: VPA (LONG + SHORT) ────────────────────────────────────
def run_vpa(symbol, regime, tf="M5"):
    cfg = VPA_CONFIG
    try:
        bars = get_candles(symbol, tf, 40)
        if len(bars) < 25: return {}
        volumes = [b["volume"] for b in bars]; closes = [b["close"] for b in bars]
        opens = [b["open"] for b in bars]; highs = [b["high"] for b in bars]; lows = [b["low"] for b in bars]
        if all(v == 0 for v in volumes[-5:]): return {}

        avg_vol = sum(volumes[-cfg["volume_avg_period"]:]) / cfg["volume_avg_period"]
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 0
        price = closes[-1]; bar_range = highs[-1] - lows[-1]
        if bar_range == 0: return {}
        close_ratio = (closes[-1] - lows[-1]) / bar_range
        price_move = bar_range / price if price > 0 else 0
        adx = calc_adx(bars); trending = adx >= cfg["adx_min"]
        ema20 = calc_ema(closes, 20)

        long_score = 0; short_score = 0
        long_sigs = []; short_sigs = []

        if trending:
            # Bullish signals
            if vol_ratio >= cfg["volume_spike_mult"] and close_ratio >= cfg["min_close_ratio"]:
                long_score += 2; long_sigs.append("VOL_SPIKE_BULL")
            if vol_ratio >= 2.5 and price_move < cfg["effort_result_ratio"] and closes[-1] > opens[-1]:
                long_score += 2; long_sigs.append("ABSORPTION_BULL")
            if vol_ratio < 0.7 and closes[-1] > opens[-1] and close_ratio > 0.5 and len(long_sigs) > 0:
                long_score += 1; long_sigs.append("NO_SUPPLY")
            if ema20 and price > ema20[-1]: long_score += 1

            # Bearish signals — MIRROR
            if vol_ratio >= cfg["volume_spike_mult"] and close_ratio <= (1 - cfg["min_close_ratio"]):
                short_score += 2; short_sigs.append("VOL_SPIKE_BEAR")
            if vol_ratio >= 2.5 and price_move < cfg["effort_result_ratio"] and closes[-1] < opens[-1]:
                short_score += 2; short_sigs.append("ABSORPTION_BEAR")
            if vol_ratio < 0.7 and closes[-1] < opens[-1] and close_ratio < 0.5 and len(short_sigs) > 0:
                short_score += 1; short_sigs.append("NO_DEMAND")
            if ema20 and price < ema20[-1]: short_score += 1

        # Bear regime cap
        if regime == "BEAR" and long_score > cfg["bear_score_cap"]: long_score = cfg["bear_score_cap"]
        if regime == "BULL" and short_score > cfg["bear_score_cap"]: short_score = cfg["bear_score_cap"]

        sig = {"price": price, "vol_ratio": round(vol_ratio,2), "adx": round(adx,1),
               "trending": trending, "close_ratio": round(close_ratio,2),
               "long_score": long_score, "short_score": short_score,
               "long_sigs": long_sigs, "short_sigs": short_sigs, "strategy": "VPA"}
        bot_state["signals"][symbol]["VPA" if tf=="M5" else "VPA_15m"] = sig
        log.info(f"[VPA] {symbol} | vol={round(vol_ratio,2)}x ADX={round(adx,1)} L={long_score} S={short_score} sigs={long_sigs+short_sigs}")
        return sig
    except Exception as e: log.error(f"[VPA] error {symbol}: {e}"); return {}

# ── STRATEGY D: BREAKOUT (LONG + SHORT) ────────────────────────────────
def run_breakout(symbol, regime, tf="M5"):
    cfg = BREAKOUT_CONFIG
    try:
        bars = get_candles(symbol, tf, 40)
        if len(bars) < 12: return {}
        closes = [b["close"] for b in bars]; highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]; volumes = [b["volume"] for b in bars]
        if all(v == 0 for v in volumes[-5:]): return {}

        price = closes[-1]; pv = pip_value(symbol)
        avg_vol = sum(volumes[-20:]) / len(volumes[-20:]) if volumes[-20:] else 1
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 0
        adx = calc_adx(bars); trending = adx >= cfg["adx_min"]

        lookback = cfg["consolidation_candles"]
        consol = bars[-(lookback+2):-2]
        c_highs = [b["high"] for b in consol]; c_lows = [b["low"] for b in consol]
        c_range_pips = pips(symbol, max(c_highs) - min(c_lows))
        c_high = max(c_highs); c_low = min(c_lows)
        in_consol = c_range_pips <= cfg["consolidation_pips"]

        bar_range = highs[-1] - lows[-1]
        close_ratio = (closes[-1] - lows[-1]) / bar_range if bar_range > 0 else 0

        # Bull breakout
        bo_pips_up = pips(symbol, closes[-1] - c_high)
        prev = bars[-2]; prev_range = prev["high"] - prev["low"]
        prev_bull = False; prev_bear = False
        if prev_range > 0:
            pcr = (prev["close"] - prev["low"]) / prev_range
            prev_bull = prev["close"] > c_high and pcr >= 0.5
            prev_bear = prev["close"] < c_low and pcr <= 0.5

        bull_breakout = (in_consol and closes[-1] > c_high and bo_pips_up >= cfg["min_breakout_pips"]
                        and vol_ratio >= cfg["breakout_volume_mult"]
                        and close_ratio >= cfg["breakout_candle_close_ratio"] and prev_bull and trending)

        # Bear breakdown — MIRROR
        bo_pips_down = pips(symbol, c_low - closes[-1])
        bear_breakout = (in_consol and closes[-1] < c_low and bo_pips_down >= cfg["min_breakout_pips"]
                        and vol_ratio >= cfg["breakout_volume_mult"]
                        and close_ratio <= (1 - cfg["breakout_candle_close_ratio"]) and prev_bear and trending)

        long_score = 4 if bull_breakout else 0
        short_score = 4 if bear_breakout else 0

        sig = {"price": price, "vol_ratio": round(vol_ratio,2), "adx": round(adx,1),
               "trending": trending, "consol_pips": round(c_range_pips,1),
               "bull_breakout": bull_breakout, "bear_breakout": bear_breakout,
               "long_score": long_score, "short_score": short_score, "strategy": "Breakout"}
        bot_state["signals"][symbol]["Breakout" if tf=="M5" else "Breakout_15m"] = sig
        log.info(f"[Breakout] {symbol} | ADX={round(adx,1)} consol={round(c_range_pips,1)}p bullBO={bull_breakout} bearBO={bear_breakout}")
        return sig
    except Exception as e: log.error(f"[Breakout] error {symbol}: {e}"); return {}

def run_sweep(symbol, regime, tf="M5"):
    """Liquidity Sweep Reversal (Smart Money Concept).
    Detects when price spikes PAST a recent swing high/low (grabbing stop-order
    liquidity) then FAILS and closes back on the other side — a reversal signal.

    Bullish sweep: price wicks below a recent swing low, then closes back above it
                   → stops below the low got run, buyers step in → LONG
    Bearish sweep: price wicks above a recent swing high, then closes back below it
                   → stops above the high got run, sellers step in → SHORT

    Deliberately does NOT use an ADX trend filter — this trades reversals, not trends.
    """
    cfg = SWEEP_CONFIG
    try:
        bars = get_candles(symbol, tf, 40)
        if len(bars) < cfg["swing_lookback"] + 3: return {}
        closes = [b["close"] for b in bars]; highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]; opens = [b["open"] for b in bars]
        volumes = [b["volume"] for b in bars]
        if all(v == 0 for v in volumes[-5:]): return {}

        price = closes[-1]; pv = pip_value(symbol)
        avg_vol = sum(volumes[-20:]) / len(volumes[-20:]) if volumes[-20:] else 1
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 0

        # Find the swing high/low over the lookback window, EXCLUDING the last 2 bars
        # (the sweep itself is the last bar, so we look at the range it swept into)
        window = bars[-(cfg["swing_lookback"]+2):-2]
        swing_high = max(b["high"] for b in window)
        swing_low  = min(b["low"] for b in window)

        cur = bars[-1]
        cur_high = cur["high"]; cur_low = cur["low"]
        cur_close = cur["close"]; cur_open = cur["open"]
        bar_range = cur_high - cur_low
        if bar_range == 0: return {}
        close_ratio = (cur_close - cur_low) / bar_range

        # BULLISH SWEEP: wick pierced below swing low, but closed back ABOVE it
        swept_low_pips = pips(symbol, swing_low - cur_low)   # how far below the low we poked
        bull_sweep = (cur_low < swing_low                                   # wicked below
                      and cur_close > swing_low                             # closed back above
                      and cfg["min_sweep_pips"] <= swept_low_pips <= cfg["max_sweep_pips"]
                      and close_ratio >= 0.5                                # closed in upper half
                      and vol_ratio >= cfg["volume_mult"])                  # elevated volume (stop run)

        # BEARISH SWEEP: wick pierced above swing high, but closed back BELOW it
        swept_high_pips = pips(symbol, cur_high - swing_high)
        bear_sweep = (cur_high > swing_high                                 # wicked above
                      and cur_close < swing_high                            # closed back below
                      and cfg["min_sweep_pips"] <= swept_high_pips <= cfg["max_sweep_pips"]
                      and close_ratio <= 0.5                                # closed in lower half
                      and vol_ratio >= cfg["volume_mult"])

        long_score = 4 if bull_sweep else 0
        short_score = 4 if bear_sweep else 0

        sig = {"price": price, "vol_ratio": round(vol_ratio,2),
               "swing_high": round(swing_high,5), "swing_low": round(swing_low,5),
               "bull_sweep": bull_sweep, "bear_sweep": bear_sweep,
               "long_score": long_score, "short_score": short_score, "strategy": "Sweep"}
        bot_state["signals"][symbol]["Sweep" if tf=="M5" else "Sweep_15m"] = sig
        if bull_sweep or bear_sweep:
            log.info(f"[Sweep] {symbol} | bullSweep={bull_sweep} bearSweep={bear_sweep} vol={round(vol_ratio,1)}x")
        return sig
    except Exception as e: log.error(f"[Sweep] error {symbol}: {e}"); return {}

# ── EXIT / ENTRY ───────────────────────────────────────────────────────
def check_exits(symbol, now):
    pos = bot_state["positions"].get(symbol)
    if not pos: return
    entry = pos["entry"]; units = pos["units"]; strategy = pos.get("strategy", "UNKNOWN")
    trade_id = pos.get("trade_id", ""); side = pos.get("side", "long")
    bars = get_candles(symbol, "M5", 3)
    if not bars: return
    price = bars[-1]["close"]

    if side == "long":
        pnl_pips = (price - entry) / pip_value(symbol)
    else:
        pnl_pips = (entry - price) / pip_value(symbol)

    should_exit = False; reason = ""
    if pnl_pips >= RISK["take_profit_pips"]:
        should_exit = True; reason = f"Take profit (+{round(pnl_pips,1)}p)"
    elif pnl_pips <= -RISK["stop_loss_pips"]:
        should_exit = True; reason = f"Stop loss ({round(pnl_pips,1)}p)"
        bot_state["active_cooldowns"][f"{strategy}_{symbol}"] = now.isoformat()

    if should_exit:
        exit_price = close_position(symbol, trade_id)
        if exit_price:
            if side == "long":
                pnl = calc_pnl(symbol, entry, exit_price, units)
            else:
                pnl = calc_pnl(symbol, exit_price, entry, units)
            win = pnl > 0
            record_exit(symbol, strategy, side, pnl, win)
            side_label = "LONG" if side == "long" else "SHORT"
            add_diary(symbol,
                f"{'WIN' if win else 'LOSS'} [{side_label}] | {entry:.5f}→{exit_price:.5f} | "
                f"{round(pnl_pips,1)}p | ${round(pnl,2)} | {reason}",
                "win" if win else "loss", strategy)
            bot_state["closed_trades"].append({"symbol": symbol, "side": side,
                "entry": entry, "exit": exit_price, "pnl": round(pnl,2),
                "pips": round(pnl_pips,1), "win": win, "strategy": strategy,
                "reason": reason, "time": now.strftime("%H:%M")})
            sync_positions()

def try_entry(symbol, strategy, sig, regime, side, now):
    if not can_enter(symbol, strategy, side): return
    if not is_trading_window({"EMA": EMA_CONFIG, "MSS": MSS_CONFIG, "VPA": VPA_CONFIG,
                              "Breakout": BREAKOUT_CONFIG}.get(strategy, {})): return

    score_key = "long_score" if side == "BUY" else "short_score"
    score = sig.get(score_key, 0)
    cfg = {"EMA": EMA_CONFIG, "MSS": MSS_CONFIG, "VPA": VPA_CONFIG, "Breakout": BREAKOUT_CONFIG, "Sweep": SWEEP_CONFIG}.get(strategy, {})
    regime = bot_state["market_regime"].get(symbol, "UNKNOWN")

    # FIX: Asymmetric thresholds — favor the trend direction
    # BEAR: shorts need 3, longs need 5 (shorts are with the trend)
    # BULL: longs need 3, shorts need 5 (longs are with the trend)
    # EXCEPTION: Sweep is a REVERSAL strategy by design — it deliberately trades
    # counter-trend liquidity grabs, so it uses a flat threshold of 4 both directions.
    # Applying the asymmetric trend filter would block the exact setups it exists to catch.
    if strategy == "Sweep":
        min_score = cfg.get("min_score", 4)
    elif regime == "BEAR":
        min_score = 3 if side == "SELL" else 5
    elif regime == "BULL":
        min_score = 3 if side == "BUY" else 5
    else:
        min_score = cfg.get("min_score", 4)

    if score < min_score:
        return

    order_side = side
    # FIX: Reduce position size when trading against the trend
    regime = bot_state["market_regime"].get(symbol, "UNKNOWN")
    with_trend = (regime == "BEAR" and side == "SELL") or (regime == "BULL" and side == "BUY")
    units = RISK["position_units"] if with_trend else RISK.get("counter_trend_units", 2500)

    # Calculate TP and SL prices for server-side execution
    pv = pip_value(symbol)
    if side == "BUY":
        tp_price = None  # Will be set after we know entry price
        sl_price = None
    else:
        tp_price = None
        sl_price = None

    entry_price = place_order(symbol, units, order_side)
    if entry_price:
        pv = pip_value(symbol)
        precision = 3 if "JPY" in symbol else 5
        if side == "BUY":
            tp = round(entry_price + RISK["take_profit_pips"] * pv, precision)
            sl = round(entry_price - RISK["stop_loss_pips"] * pv, precision)
        else:
            tp = round(entry_price - RISK["take_profit_pips"] * pv, precision)
            sl = round(entry_price + RISK["stop_loss_pips"] * pv, precision)

        # Set server-side SL/TP on the open trade
        try:
            sync_positions()
            pos = bot_state["positions"].get(symbol, {})
            trade_id = pos.get("trade_id", "")
            if trade_id and trade_id != "pending":
                import oandapyV20.endpoints.trades as trades_ep
                client = get_oanda_client()
                sl_tp_data = {
                    "stopLoss": {"price": str(round(sl, precision))},
                    "takeProfit": {"price": str(round(tp, precision))}
                }
                trades_ep.TradeCRCDO(OANDA_ACCOUNT_ID, trade_id, sl_tp_data)
                log.info(f"Server-side SL/TP set: SL={sl} TP={tp}")
        except Exception as e:
            log.error(f"SL/TP set error: {e}")

        side_word = "long" if side == "BUY" else "short"
        bot_state["positions"][symbol] = {"symbol": symbol, "entry": entry_price,
            "units": units, "side": side_word, "trade_id": "pending",
            "open_time": now.isoformat(), "current_price": entry_price,
            "unrealized_pnl": 0, "strategy": strategy}
        bot_state["strategy_positions"][strategy].append(symbol)
        sync_positions()
        side_label = "BUY" if side == "BUY" else "SHORT"
        add_diary(symbol,
            f"{side_label} | {entry_price:.5f} | Score {score} | "
            f"TP {tp} | SL {sl} | ADX {sig.get('adx', 0)}",
            "trade", strategy)
        log.info(f"[{strategy}] {side_label} {symbol} at {entry_price} | score={score} | ADX={sig.get('adx',0)}")

# ── TRADING LOOP ───────────────────────────────────────────────────────
def trading_loop():
    if not OANDA_API_KEY or not OANDA_ACCOUNT_ID:
        log.warning("No OANDA credentials"); return

    add_diary("SYSTEM",
        "ForexAI v8.2 started (+EMA long filters: no bear longs, ADX>=25) | LONG + SHORT enabled | "
        "ADX trend filter | 7 Pairs | 4 Strategies | "
        "M5+M15 scanning | No time exit",
        "system")
    log.info("ForexAI Combined Bot v8.2 started")
    send_telegram("🚀 <b>Forex Bot v7.0 started</b>\nLONG + SHORT enabled | ADX filter | 7 pairs")

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

            # Daily loss check
            if bot_state["daily_start_nav"] > 0:
                loss_pct = (bot_state["daily_start_nav"] - bot_state["account_nav"]) / bot_state["daily_start_nav"] * 100
                if loss_pct >= RISK["daily_loss_limit_pct"] and not bot_state["daily_paused"]:
                    bot_state["daily_paused"] = True
                    add_diary("SYSTEM", f"Daily loss limit hit", "system")
            if bot_state["daily_paused"]: time.sleep(60); continue

            for symbol in SYMBOLS:
                if bot_state["killed"]: break
                regime = bot_state["market_regime"].get(symbol, "UNKNOWN")
                check_exits(symbol, now)

                for strat, run_fn in [("Breakout", run_breakout), ("EMA", run_ema),
                                      ("MSS", run_mss), ("VPA", run_vpa), ("Sweep", run_sweep)]:
                    if len(bot_state["strategy_positions"][strat]) < RISK["max_positions_per_strategy"]:
                        for tf in ["M5", "M15"]:
                            sig = run_fn(symbol, regime, tf)
                            if not sig: continue
                            # Try LONG
                            if sig.get("long_score", 0) >= 4:
                                try_entry(symbol, strat, sig, regime, "BUY", now)
                            # Try SHORT
                            if sig.get("short_score", 0) >= 4:
                                try_entry(symbol, strat, sig, regime, "SELL", now)
                            if len(bot_state["strategy_positions"][strat]) >= RISK["max_positions_per_strategy"]:
                                break

        except Exception as e:
            log.error(f"Loop error: {e}")
            import traceback; log.error(traceback.format_exc())
        time.sleep(60)

threading.Thread(target=trading_loop, daemon=True).start()

# ── Flask routes ───────────────────────────────────────────────────────
@app.after_request
def no_cache(r):
    r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"; return r

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
    get_account_info(); wins = bot_state["win_count"]; total = bot_state["total_trades"]
    return jsonify(clean_val({"running": bot_state["running"], "killed": bot_state["killed"],
        "paper_mode": PAPER_MODE, "version": bot_state["version"],
        "market_open": bot_state["market_open"], "in_trading_window": bot_state["in_trading_window"],
        "positions": bot_state["positions"], "strategy_positions": bot_state["strategy_positions"],
        "closed_trades": bot_state["closed_trades"][-50:], "diary": bot_state["diary"][-100:],
        "day_pnl": bot_state["day_pnl"], "total_trades": total,
        "win_rate": round(wins/total*100) if total > 0 else 0,
        "strategy_stats": bot_state["strategy_stats"],
        "long_stats": bot_state["long_stats"], "short_stats": bot_state["short_stats"],
        "signals": bot_state["signals"],
        "account_balance": bot_state["account_balance"], "account_equity": bot_state["account_equity"],
        "account_nav": bot_state["account_nav"], "market_regime": bot_state["market_regime"],
        "active_cooldowns": bot_state["active_cooldowns"], "daily_paused": bot_state["daily_paused"]}))

@app.route("/diary")
def diary():
    sf = request.args.get("strategy"); entries = bot_state["diary"]
    if sf: entries = [e for e in entries if e.get("strategy") == sf]
    return jsonify({"diary": entries})

@app.route("/kill", methods=["POST"])
def kill():
    bot_state["killed"] = not bot_state["killed"]
    add_diary("SYSTEM", f"Kill switch {'KILLED' if bot_state['killed'] else 'RESUMED'}", "system")
    return jsonify({"killed": bot_state["killed"]})

@app.route("/bars")
def bars_route():
    symbol = request.args.get("symbol", "EUR_USD"); tf = request.args.get("timeframe", "M5")
    candles = get_candles(symbol, tf, 150); result = []
    for c in candles:
        try:
            t = int(datetime.fromisoformat(c["time"].replace("Z","+00:00")).timestamp())
            result.append({"time": t, "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"]})
        except: pass
    return jsonify(result)

@app.route("/history")
def history():
    sf = request.args.get("strategy"); trades = bot_state["closed_trades"]
    if sf: trades = [t for t in trades if t.get("strategy") == sf]
    return jsonify({"trades": trades})

@app.route("/")
def index():
    try:
        with open("index.html") as f: return f.read()
    except: return jsonify({"status": "ForexAI v7.0", "symbols": SYMBOLS})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080)); app.run(host="0.0.0.0", port=port)
