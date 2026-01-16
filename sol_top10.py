import os
import json
import time
import math
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta
from pathlib import Path

# === Force paths relative to this script file ===
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR              # change to .parent if script is inside /src
OUTPUT_DIR = REPO_ROOT / "output"
CACHE_PATH = REPO_ROOT / "candidate_cache.csv"

# -----------------------------
# LOAD CONFIG
# -----------------------------
with open(REPO_ROOT / "config.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

CHAIN_ID = str(CONFIG.get("chain_id", "solana")).lower()
LIQUIDITY_MIN = float(CONFIG.get("liquidity_min_usd", 25000))
MIN_VOL_24H = float(CONFIG.get("min_volume_24h_usd", 200000))
MAX_CANDIDATES = int(CONFIG.get("max_candidates", 5000))
LOOKBACK_DAYS = int(CONFIG.get("lookback_days", 30))
CG_KEY = CONFIG.get("coingecko_api_key")

BLACKLIST = set(CONFIG.get("blacklist_tokens", []))

if not CG_KEY or "PUT_YOUR_COINGECKO_KEY_HERE" in str(CG_KEY):
    raise SystemExit("Put your CoinGecko key into config.json as coingecko_api_key")


# Launch-age filtering
MAX_LAUNCH_AGE_DAYS = float(CONFIG.get("max_launch_age_days", 7))

# Ignore pairs-count guard for tokens launched within this many days
PAIRS_GUARD_IGNORE_DAYS = float(CONFIG.get("pairs_guard_ignore_days", 30))

# Allowlists for "valid" pairs
dex_allow = set(x.lower() for x in CONFIG.get("dex_allowlist", []) if isinstance(x, str))
quote_allow = set(x.upper() for x in CONFIG.get("quote_allowlist", []) if isinstance(x, str))
dex_allow = dex_allow if dex_allow else None
quote_allow = quote_allow if quote_allow else None

# Launch inference thresholds (separate from final LIQUIDITY_MIN / MIN_VOL_24H)
LAUNCH_MIN_LIQ = float(CONFIG.get("launch_min_liquidity_usd", 7500))
LAUNCH_MIN_VOL = float(CONFIG.get("launch_min_volume_24h_usd", 15000))
LAUNCH_FB_MIN_LIQ = float(CONFIG.get("launch_fallback_min_liquidity_usd", 2000))
LAUNCH_FB_MIN_VOL = float(CONFIG.get("launch_fallback_min_volume_24h_usd", 3000))

# Pairs-count guardrail
MAX_PAIRS_FOR_NEW = int(CONFIG.get("max_pairs_for_new", 0) or 0)
PAIRS_GUARD_MODE = str(CONFIG.get("pairs_guard_mode", "exclude")).lower()  # exclude|penalize|off

# -----------------------------
# PATHS (forced inside repo)
# -----------------------------
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# -----------------------------
# ENDPOINTS
# -----------------------------
CG_NEW_POOLS = "https://api.coingecko.com/api/v3/onchain/networks/solana/new_pools"

DS_TOKEN_PROFILES_LATEST = "https://api.dexscreener.com/token-profiles/latest/v1"
DS_COMMUNITY_TAKEOVERS_LATEST = "https://api.dexscreener.com/community-takeovers/latest/v1"
DS_SEARCH = "https://api.dexscreener.com/latest/dex/search?q={q}"
DS_TOKEN_PAIRS = "https://api.dexscreener.com/token-pairs/v1/{chain}/{token}"
DS_TOKEN_BOOSTS_LATEST = "https://api.dexscreener.com/token-boosts/latest/v1"
DS_TOKEN_BOOSTS_TOP = "https://api.dexscreener.com/token-boosts/top/v1"

# -----------------------------
# HTTP SESSIONS
# -----------------------------
CG = requests.Session()
CG.headers.update({
    "Accept": "application/json",
    "x-cg-demo-api-key": CG_KEY,
    "User-Agent": "Mozilla/5.0"
})

DS = requests.Session()
DS.headers.update({
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0"
})

def get_json(session: requests.Session, url: str, params=None):
    r = session.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def safe_float(x):
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None

def liq_usd(pair: dict) -> float:
    liq = pair.get("liquidity") or {}
    return float(liq.get("usd") or 0.0)

def pick_best_pair(pairs):
    if not pairs:
        return None
    return max(pairs, key=liq_usd)

def get_price_usd(pair: dict):
    return safe_float(pair.get("priceUsd"))

def get_volume_24h(pair: dict):
    vol = pair.get("volume") or {}
    return safe_float(vol.get("h24")) or 0.0

def get_mcap_or_fdv(pair: dict):
    mcap = safe_float(pair.get("marketCap"))
    fdv = safe_float(pair.get("fdv"))
    if mcap and mcap > 0:
        return mcap, "marketCap"
    if fdv and fdv > 0:
        return fdv, "fdv"
    return None, None

def extract_token_mints_from_cg_pool_item(item: dict):
    attrs = item.get("attributes") or {}
    candidates = []

    for k in (
        "base_token_address", "baseTokenAddress", "base_token_id",
        "quote_token_address", "quoteTokenAddress", "quote_token_id",
        "token0_address", "token1_address"
    ):
        v = attrs.get(k)
        if isinstance(v, str) and len(v) > 20:
            candidates.append(v)

    for k in ("base_token", "quote_token", "token0", "token1"):
        obj = attrs.get(k)
        if isinstance(obj, dict):
            v = obj.get("address") or obj.get("id")
            if isinstance(v, str) and len(v) > 20:
                candidates.append(v)

    seen = set()
    out = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out

# -----------------------------
# LAUNCH INFERENCE HELPERS
# -----------------------------
def norm_pair_created_at(ts):
    """Return pairCreatedAt in seconds (int) or None."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        ts = int(ts)
        if ts <= 0:
            return None
        # DexScreener often uses ms
        if ts > 10_000_000_000:
            ts = ts // 1000
        return ts
    return None

def get_quote_symbol(pair: dict) -> str:
    qt = pair.get("quoteToken") or {}
    sym = qt.get("symbol") or ""
    return str(sym).upper()

def is_pair_allowed(pair: dict, dex_allow_set, quote_allow_set) -> bool:
    if str(pair.get("chainId", "")).lower() != "solana":
        return False
    if dex_allow_set:
        dex = str(pair.get("dexId", "")).lower()
        if dex not in dex_allow_set:
            return False
    if quote_allow_set:
        if get_quote_symbol(pair) not in quote_allow_set:
            return False
    return True

def infer_launch_from_pairs(
    pairs,
    dex_allow_set,
    quote_allow_set,
    strict_min_liq,
    strict_min_vol,
    fallback_min_liq,
    fallback_min_vol
):
    """
    Infer token launch timestamp from earliest *valid* pairCreatedAt.
    Returns: (launch_ts_seconds or None, launch_pair or None, confidence: 'high'|'medium'|'low')
    """
    if not pairs:
        return None, None, "low"

    def passes(pair, min_liq, min_vol):
        if not is_pair_allowed(pair, dex_allow_set, quote_allow_set):
            return False
        ts = norm_pair_created_at(pair.get("pairCreatedAt"))
        if ts is None:
            return False
        if liq_usd(pair) < min_liq:
            return False
        if get_volume_24h(pair) < min_vol:
            return False
        return True

    strict = [p for p in pairs if passes(p, strict_min_liq, strict_min_vol)]
    if strict:
        lp = min(strict, key=lambda p: norm_pair_created_at(p.get("pairCreatedAt")))
        return norm_pair_created_at(lp.get("pairCreatedAt")), lp, "high"

    fb = [p for p in pairs if passes(p, fallback_min_liq, fallback_min_vol)]
    if fb:
        lp = min(fb, key=lambda p: norm_pair_created_at(p.get("pairCreatedAt")))
        return norm_pair_created_at(lp.get("pairCreatedAt")), lp, "medium"

    return None, None, "low"

def pick_best_pair_filtered(pairs, dex_allow_set, quote_allow_set):
    allowed = [p for p in pairs if is_pair_allowed(p, dex_allow_set, quote_allow_set)]
    return pick_best_pair(allowed) if allowed else pick_best_pair(pairs)

# -----------------------------
# CACHE (Token Address + first_seen)
# -----------------------------
def load_cache_df():
    cols = ["Token Address", "first_seen", "launch_ts", "launch_pair", "launch_liquidity_usd"]
    cache_path = Path(CACHE_PATH)
    if cache_path.is_file():
        try:
            df = pd.read_csv(cache_path, dtype=str)
            # Ensure required columns exist
            for c in cols:
                if c not in df.columns:
                    df[c] = None
            return df[cols]
        except Exception:
            pass
    return pd.DataFrame(columns=cols)


def upsert_first_seen(cache_df: pd.DataFrame, token_addrs):
    # Ensure schema
    for c in ["Token Address", "first_seen", "launch_ts", "launch_pair", "launch_liquidity_usd"]:
        if c not in cache_df.columns:
            cache_df[c] = None

    existing = set(cache_df["Token Address"].tolist())
    new_tokens = [t for t in token_addrs if t not in existing]
    if new_tokens:
        add_df = pd.DataFrame({
            "Token Address": new_tokens,
            "first_seen": [now_iso()] * len(new_tokens),
            "launch_ts": [None] * len(new_tokens),
            "launch_pair": [None] * len(new_tokens),
            "launch_liquidity_usd": [None] * len(new_tokens),
        })
        cache_df = pd.concat([cache_df, add_df], ignore_index=True)
    return cache_df


# -----------------------------
# DISCOVERY
# -----------------------------
def discover_from_coingecko(candidate_set):
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    page = 1
    MAX_CG_PAGES = 10  # demo tier often limited beyond this
    pools_scanned = 0

    while page <= MAX_CG_PAGES:
        payload = get_json(CG, CG_NEW_POOLS, params={"page": page})
        data = payload.get("data") or []
        if not data:
            break

        for item in data:
            pools_scanned += 1
            attrs = item.get("attributes") or {}
            created_at = attrs.get("created_at") or attrs.get("createdAt")
            if created_at:
                try:
                    dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    if dt < cutoff:
                        return pools_scanned
                except Exception:
                    pass

            mints = extract_token_mints_from_cg_pool_item(item)
            for m in mints:
                if m and m not in BLACKLIST:
                    candidate_set.add(m)

        page += 1
        time.sleep(0.2)

    return pools_scanned

def discover_from_dexscreener_feeds(candidate_set):
    prof_added = 0
    take_added = 0

    profiles = get_json(DS, DS_TOKEN_PROFILES_LATEST)
    if isinstance(profiles, list):
        for p in profiles:
            if str(p.get("chainId", "")).lower() == "solana":
                addr = p.get("tokenAddress") or p.get("address")
                if addr and addr not in BLACKLIST:
                    before = len(candidate_set)
                    candidate_set.add(addr)
                    if len(candidate_set) > before:
                        prof_added += 1

    takeovers = get_json(DS, DS_COMMUNITY_TAKEOVERS_LATEST)
    if isinstance(takeovers, list):
        for t in takeovers:
            chain = str(t.get("chainId", "")).lower()
            addr = t.get("tokenAddress") or t.get("address")
            if chain == "solana" and addr and addr not in BLACKLIST:
                before = len(candidate_set)
                candidate_set.add(addr)
                if len(candidate_set) > before:
                    take_added += 1

            tok = t.get("token") or {}
            if isinstance(tok, dict):
                chain2 = str(tok.get("chainId", "")).lower()
                addr2 = tok.get("address") or tok.get("tokenAddress")
                if chain2 == "solana" and addr2 and addr2 not in BLACKLIST:
                    before = len(candidate_set)
                    candidate_set.add(addr2)
                    if len(candidate_set) > before:
                        take_added += 1

    return prof_added, take_added

def discover_from_search(candidate_set):
    search_terms = [
        # pump / launch patterns
        "pump", ".pump", "pumpfun", "pump fun", "launch", "cto",

        # common note-worthy words
        "ai", "anime", "cat", "dog", "pepe", "wif", "bonk", "meme",

        # culture / trends
        "minecraft", "grandma", "fund", "psyop", "sigma", "based", "elon",

        # solana ecosystem
        "raydium", "orca", "meteora", "jupiter", "sol", "solana",

        # trader keywords
        "trend", "trending", "volume", "breakout", "runner"
    ]

    added = 0
    for term in search_terms:
        try:
            payload = get_json(DS, DS_SEARCH.format(q=term))
            pairs = payload.get("pairs") or []
            for p in pairs:
                if str(p.get("chainId", "")).lower() != "solana":
                    continue
                base = p.get("baseToken") or {}
                addr = base.get("address")
                if addr and addr not in BLACKLIST and len(addr) > 20:
                    before = len(candidate_set)
                    candidate_set.add(addr)
                    if len(candidate_set) > before:
                        added += 1
            time.sleep(0.15)
        except Exception:
            continue
    return added

def discover_from_token_boosts(candidate_set):
    """
    Pull tokens from DexScreener Token Boost feeds (top + latest).
    This often catches fast movers that don't appear in profiles/takeovers/search yet.
    """
    added = 0

    def add_addr(addr):
        nonlocal added
        if addr and addr not in BLACKLIST and len(str(addr)) > 20:
            before = len(candidate_set)
            candidate_set.add(str(addr).strip())
            if len(candidate_set) > before:
                added += 1

    # latest boosts
    try:
        data = get_json(DS, DS_TOKEN_BOOSTS_LATEST)
        if isinstance(data, list):
            for it in data:
                if str(it.get("chainId", "")).lower() != "solana":
                    continue
                add_addr(it.get("tokenAddress") or it.get("address"))
    except Exception as e:
        print(f"[DEBUG] TOKEN_BOOSTS_LATEST failed: {type(e).__name__}: {e}")

    time.sleep(0.15)

    # top boosts
    try:
        data = get_json(DS, DS_TOKEN_BOOSTS_TOP)
        if isinstance(data, list):
            for it in data:
                if str(it.get("chainId", "")).lower() != "solana":
                    continue
                add_addr(it.get("tokenAddress") or it.get("address"))
    except Exception as e:
        print(f"[DEBUG] TOKEN_BOOSTS_TOP failed: {type(e).__name__}: {e}")

    return added


def is_blank(v):
    return v is None or str(v).strip() == "" or str(v).lower() in ("nan", "none")


def fmt_compact_usd(x):
    """
    Convert numbers to compact human-readable strings:
    1_000 -> 1k
    1_500 -> 1.5k
    1_000_000 -> 1m
    1_250_000 -> 1.25m
    """
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None

    abs_x = abs(x)

    if abs_x >= 1_000_000_000:
        return f"{x/1_000_000_000:.2f}b".rstrip("0").rstrip(".")
    if abs_x >= 1_000_000:
        return f"{x/1_000_000:.2f}m".rstrip("0").rstrip(".")
    if abs_x >= 1_000:
        return f"{x/1_000:.2f}k".rstrip("0").rstrip(".")
    return str(int(x)) if x.is_integer() else str(x)

def age_bucket_hours(age_h: float | None) -> str | None:
    if age_h is None:
        return None
    try:
        age_h = float(age_h)
    except Exception:
        return None

    if age_h < 0:
        return None

    if age_h <= 6:
        return "0-6h"
    if age_h <= 12:
        return "6-12h"
    if age_h <= 24:
        return "12-24h"
    if age_h <= 48:
        return "24-48h"
    if age_h <= 72:
        return "48-72h"
    if age_h <= 168:
        return "3-7d"
    return ">7d"

# -----------------------------
# ENRICH + FILTER
# -----------------------------
def enrich_and_filter(token_list, cache_df: pd.DataFrame):
    included = []
    excluded = []
    now_dt = datetime.now(timezone.utc)
    idx_map = {str(addr).strip(): i for i, addr in enumerate(cache_df["Token Address"].astype(str).tolist())}

    for mint in token_list[:MAX_CANDIDATES]:
        mint = str(mint).strip()
        if mint in BLACKLIST:
            continue

        try:
            pairs = get_json(DS, DS_TOKEN_PAIRS.format(chain=CHAIN_ID, token=mint))

            if not isinstance(pairs, list) or not pairs:
                excluded.append({"Token Address": mint, "reason": "no_pairs"})
                continue

            pairs_count = len(pairs)

            # ---- NEW: infer REAL "launch time" from earliest VALID pairCreatedAt ----
            launch_ts, launch_pair, launch_conf = infer_launch_from_pairs(
                pairs,
                dex_allow_set=dex_allow,
                quote_allow_set=quote_allow,
                strict_min_liq=LAUNCH_MIN_LIQ,
                strict_min_vol=LAUNCH_MIN_VOL,
                fallback_min_liq=LAUNCH_FB_MIN_LIQ,
                fallback_min_vol=LAUNCH_FB_MIN_VOL
            )

            if launch_ts is None:
                excluded.append({
                    "Token Address": mint,
                    "reason": "no_valid_launch_pair",
                    "pairs_count": pairs_count
                })
                continue

            # ---- Cache launch snapshot once (launch_ts, launch_pair, launch_liquidity_usd) ----
            i = idx_map.get(mint)
            if i is not None:
                if is_blank(cache_df.at[i, "launch_ts"]):
                    cache_df.at[i, "launch_ts"] = str(int(launch_ts))

                launch_pair_addr = launch_pair.get("pairAddress") if isinstance(launch_pair, dict) else None
                if is_blank(cache_df.at[i, "launch_pair"]) and launch_pair_addr:
                    cache_df.at[i, "launch_pair"] = launch_pair_addr

                if is_blank(cache_df.at[i, "launch_liquidity_usd"]):
                    snap_liq = liq_usd(launch_pair) if isinstance(launch_pair, dict) else None
                    if snap_liq is not None and snap_liq > 0:
                        cache_df.at[i, "launch_liquidity_usd"] = str(float(snap_liq))

            launch_dt = datetime.fromtimestamp(int(launch_ts), tz=timezone.utc)
            launch_age_hours = (now_dt - launch_dt).total_seconds() / 3600.0

            # --- Pairs guard only for "old" tokens ---
            launch_age_days = launch_age_hours / 24.0

            if (
                    PAIRS_GUARD_MODE != "off"
                    and MAX_PAIRS_FOR_NEW > 0
                    and pairs_count > MAX_PAIRS_FOR_NEW
                    and launch_age_days > PAIRS_GUARD_IGNORE_DAYS
            ):
                if PAIRS_GUARD_MODE == "exclude":
                    excluded.append({
                        "Token Address": mint,
                        "reason": f"too_many_pairs>{MAX_PAIRS_FOR_NEW}_and_older_than_{PAIRS_GUARD_IGNORE_DAYS}d",
                        "pairs_count": pairs_count,
                        "launch_age_days": round(launch_age_days, 2),
                        "launch_dt": launch_dt.isoformat()
                    })
                    continue

            # Guard against bad timestamps / clock skew
            if launch_age_hours < 0:
                excluded.append({
                    "Token Address": mint,
                    "reason": "negative_launch_age",
                    "launch_dt": launch_dt.isoformat()
                })
                continue

            if launch_age_hours > MAX_LAUNCH_AGE_DAYS * 24:
                excluded.append({
                    "Token Address": mint,
                    "reason": f"launch_older_than_{MAX_LAUNCH_AGE_DAYS}d",
                    "launch_dt": launch_dt.isoformat(),
                    "launch_conf": launch_conf,
                    "pairs_count": pairs_count
                })
                continue

            # Pick best pair by liquidity, preferably within allowlists
            best = pick_best_pair_filtered(pairs, dex_allow, quote_allow)
            if not best:
                excluded.append({"Token Address": mint, "reason": "no_best_pair", "pairs_count": pairs_count})
                continue

            liquidity = liq_usd(best)
            # ---- Compute liquidity growth since cached launch snapshot ----
            launch_liq_cached = None
            i = idx_map.get(mint)
            if i is not None:
                v = cache_df.at[i, "launch_liquidity_usd"]
                if not is_blank(v):
                    try:
                        launch_liq_cached = float(v)
                    except Exception:
                        launch_liq_cached = None

            liq_growth_pct = None
            if launch_liq_cached is not None and launch_liq_cached > 0:
                liq_growth_pct = (liquidity / launch_liq_cached - 1.0) * 100.0

            if liquidity < LIQUIDITY_MIN:
                excluded.append({
                    "Token Address": mint,
                    "reason": f"liquidity<{LIQUIDITY_MIN}",
                    "liquidity_usd": liquidity,
                    "liquidity_fmt": fmt_compact_usd(liquidity),
                    "pairs_count": pairs_count
                })
                continue

            vol24 = get_volume_24h(best)
            if vol24 < MIN_VOL_24H:
                excluded.append({
                    "Token Address": mint,
                    "reason": f"vol24<{MIN_VOL_24H}",
                    "vol24": vol24,
                    "vol24_fmt": fmt_compact_usd(vol24),
                    "pairs_count": pairs_count
                })
                continue

            base = best.get("baseToken") or {}
            name = base.get("name") or ""
            symbol = base.get("symbol") or ""
            price = get_price_usd(best)

            mc, mc_source = get_mcap_or_fdv(best)

            included.append({
                "Name": name,
                "Symbol": symbol,
                "Token Address": mint,
                "Pair Address": best.get("pairAddress"),
                "DEX": best.get("dexId"),
                "age_bucket": age_bucket_hours(launch_age_hours),

                "launch_liquidity_usd": launch_liq_cached,
                "liquidity_growth_pct": (round(liq_growth_pct, 2) if liq_growth_pct is not None else None),

                # Raw numeric values (keep for sorting / scoring)
                "Liquidity USD": round(liquidity, 2),
                "Volume 24h USD": round(vol24, 2),
                "Current MC/FDV USD": (round(mc, 2) if mc is not None else None),

                # Human-readable versions (for Google Sheets)
                "Liquidity_fmt": fmt_compact_usd(liquidity),
                "Volume_24h_fmt": fmt_compact_usd(vol24),
                "MC/FDV_fmt": fmt_compact_usd(mc),

                "Price USD": (round(price, 10) if price is not None else None),
                "MC Source": mc_source,

                # Debug / quality fields
                "pairs_count": pairs_count,
                "launch_dt": launch_dt.isoformat(),
                "launch_age_hours": launch_age_hours,
                "launch_conf": launch_conf,
                "launch_pair": (launch_pair.get("pairAddress") if isinstance(launch_pair, dict) else None),
                "launch_dex": (launch_pair.get("dexId") if isinstance(launch_pair, dict) else None),
                "launch_quote": (get_quote_symbol(launch_pair) if isinstance(launch_pair, dict) else None),

                # for scoring-time penalty option
                "pairs_guard_mode": PAIRS_GUARD_MODE,
                "max_pairs_for_new": MAX_PAIRS_FOR_NEW
            })


        except Exception as e:
            excluded.append({"Token Address": mint, "reason": f"error: {type(e).__name__}: {e}"})

        time.sleep(0.08)

    return pd.DataFrame(included), pd.DataFrame(excluded)

def add_wallet_hunt_signal(df: pd.DataFrame) -> pd.DataFrame:
    def l1p(x):
        try:
            return math.log1p(max(0.0, float(x)))
        except Exception:
            return 0.0

    out = df.copy()
    sig = []

    for _, r in out.iterrows():
        try:
            age_h = float(r.get("launch_age_hours"))
        except Exception:
            age_h = None

        try:
            liq = float(r.get("Liquidity USD") or 0)
        except Exception:
            liq = 0.0

        try:
            vol = float(r.get("Volume 24h USD") or 0)
        except Exception:
            vol = 0.0

        try:
            growth = float(r.get("liquidity_growth_pct"))
        except Exception:
            growth = 0.0

        try:
            pc = int(float(r.get("pairs_count") or 0))
        except Exception:
            pc = 0

        # Age boost
        age_factor = 0.0 if age_h is None else (1.0 / (1.0 + (age_h / 12.0)))

        # Core strength
        core = (l1p(vol) * 0.55) + (l1p(liq) * 0.35) + (max(0.0, min(growth, 200.0)) / 200.0 * 0.10)

        # Pair penalty
        if MAX_PAIRS_FOR_NEW and pc > MAX_PAIRS_FOR_NEW:
            core *= 0.6

        sig.append(core * age_factor)

    out["wallet_hunt_signal"] = sig
    return out

def score_df(df: pd.DataFrame):

    # log-score: volume heavy, then liquidity, then mc/fdv
    def l1p(x): return math.log1p(max(0.0, float(x)))

    scores = []
    for _, r in df.iterrows():
        liq = float(r.get("Liquidity USD", 0) or 0)
        vol = float(r.get("Volume 24h USD", 0) or 0)
        mc  = float(r.get("Current MC/FDV USD", 0) or 0)

        base_score = (l1p(vol) * 0.50) + (l1p(liq) * 0.35) + (l1p(mc) * 0.15)

        # Optional penalty: if user selects "penalize", reduce score for many pairs
        pairs_count = int(float(r.get("pairs_count", 0) or 0))
        if PAIRS_GUARD_MODE == "penalize" and MAX_PAIRS_FOR_NEW > 0 and pairs_count > MAX_PAIRS_FOR_NEW:
            base_score *= 0.65  # 35% penalty

        scores.append(base_score)

    df = df.copy()
    df["score"] = scores
    return df

def add_age_from_first_seen(df: pd.DataFrame, cache_df: pd.DataFrame):
    m = dict(zip(cache_df["Token Address"], cache_df["first_seen"]))

    def to_dt(s):
        try:
            return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except Exception:
            return None

    ages_hours = []
    first_seen_vals = []
    now_dt = datetime.now(timezone.utc)

    for addr in df["Token Address"].tolist():
        fs = m.get(addr)
        first_seen_vals.append(fs)
        dt = to_dt(fs) if fs else None
        if dt:
            ages_hours.append((now_dt - dt).total_seconds() / 3600.0)
        else:
            ages_hours.append(None)

    df = df.copy()
    df["first_seen"] = first_seen_vals
    df["age_hours"] = ages_hours
    return df

def export_for_sheets(df: pd.DataFrame) -> pd.DataFrame:
    """
    Keep only the columns useful for quickly deciding:
    'Is this token worth early-wallet hunting?'
    """
    keep = [
        "Name",
        "Symbol",
        "Token Address",
        "DEX",
        "age_bucket",
        "launch_dt",
        "launch_age_hours",
        "Liquidity_fmt",
        "Volume_24h_fmt",
        "MC/FDV_fmt",
        "liquidity_growth_pct",
        "wallet_hunt_signal",
        "pairs_count",
        "launch_conf",
    ]

    out = df.copy()
    cols = [c for c in keep if c in out.columns]
    out = out[cols]
    return out

# -----------------------------
# MAIN
# -----------------------------
print("Running token shortlist builder...")
print("REPO_ROOT:", REPO_ROOT)
print("OUTPUT_DIR:", OUTPUT_DIR)
print("CACHE_PATH:", CACHE_PATH)

cache_df = load_cache_df()
cached_set = set(cache_df["Token Address"].tolist())
candidate_set = set(cached_set)

cg_scanned = discover_from_coingecko(candidate_set)
prof_added, take_added = discover_from_dexscreener_feeds(candidate_set)
search_added = discover_from_search(candidate_set)
boost_added = discover_from_token_boosts(candidate_set)

# Update cache (first_seen)
cache_df = upsert_first_seen(cache_df, candidate_set)
cache_df.to_csv(CACHE_PATH, index=False)

print(f"CoinGecko pools scanned (<=10 pages): {cg_scanned}")
print(f"DexScreener added (profiles):        {prof_added}")
print(f"DexScreener added (takeovers):       {take_added}")
print(f"DexScreener added (search):          {search_added}")
print(f"DexScreener added (token boosts):    {boost_added}")
print(f"Candidate cache size:                {len(cache_df)}")

# Enrich/filter from cache (so you keep improving)
token_list = cache_df["Token Address"].tolist()
df_in, df_ex = enrich_and_filter(token_list, cache_df)
cache_df.to_csv(CACHE_PATH, index=False)

df_in.to_csv(OUTPUT_DIR / "included.csv", index=False)
df_ex.to_csv(OUTPUT_DIR / "excluded.csv", index=False)

if df_in.empty:
    print("No tokens passed filters.")
    raise SystemExit(0)

# keep first_seen fields for debugging (not used for shortlist windows anymore)
df_in = add_age_from_first_seen(df_in, cache_df)
df_in = score_df(df_in)
df_in = add_wallet_hunt_signal(df_in)

print("\nDEBUG launch age stats:")
print("Max launch_age_hours:", df_in["launch_age_hours"].max())
print("Tokens older than 72h:", (df_in["launch_age_hours"] > 72).sum())
print("Tokens older than 7d:", (df_in["launch_age_hours"] > 168).sum())

# -----------------------------
# SHORTLIST WINDOWS (STRICTLY BY REAL LAUNCH AGE)
# -----------------------------
# -----------------------------
# SHORTLIST WINDOWS (STRICTLY BY REAL LAUNCH AGE)
# -----------------------------
H24 = 24
H7D = 24 * 7
H30D = 24 * 30

df_0_24 = df_in[(df_in["launch_age_hours"].notna()) & (df_in["launch_age_hours"] <= H24)].copy()

df_24_72 = df_in[
    (df_in["launch_age_hours"].notna()) &
    (df_in["launch_age_hours"] > H24) &
    (df_in["launch_age_hours"] <= 72)
].copy()

df_72_7d = df_in[
    (df_in["launch_age_hours"].notna()) &
    (df_in["launch_age_hours"] > 72) &
    (df_in["launch_age_hours"] <= H7D)
].copy()

# NEW: 7d - 30d bucket
df_7d_30d = df_in[
    (df_in["launch_age_hours"].notna()) &
    (df_in["launch_age_hours"] > H7D) &
    (df_in["launch_age_hours"] <= H30D)
].copy()

# Optional: sort by wallet_hunt_signal descending
df_0_24 = df_0_24.sort_values("wallet_hunt_signal", ascending=False)
df_24_72 = df_24_72.sort_values("wallet_hunt_signal", ascending=False)
df_72_7d = df_72_7d.sort_values("wallet_hunt_signal", ascending=False)
df_7d_30d = df_7d_30d.sort_values("wallet_hunt_signal", ascending=False)

# Export sheets-friendly CSVs
export_for_sheets(df_0_24).to_csv(OUTPUT_DIR / "tokens_0_24h.csv", index=False)
export_for_sheets(df_24_72).to_csv(OUTPUT_DIR / "tokens_24_72h.csv", index=False)
export_for_sheets(df_72_7d).to_csv(OUTPUT_DIR / "tokens_72h_7d.csv", index=False)
export_for_sheets(df_7d_30d).to_csv(OUTPUT_DIR / "tokens_7d_30d.csv", index=False)

print("\nWrote shortlists to:", OUTPUT_DIR)
print("  tokens_0_24h.csv")
print("  tokens_24_72h.csv")
print("  tokens_72h_7d.csv")
print("  tokens_7d_30d.csv")
print("\nTip: review tokens_0_24h first for early buyer hunts.")


