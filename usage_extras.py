"""Token-spend analytics for the Claude usage tray.

Adds, on top of the raw utilization endpoint:

  1. History       — every poll's utilization stored in a local SQLite DB.
  2. Burn rate     — linear projection of when each window hits 100%.
  3. Real tokens   — actual input/output/cache counts parsed from the
                     Claude Code session logs (~/.claude/projects/*/*.jsonl).
  4. Notifications — threshold-crossing toasts (caller uses pystray.notify).
  5. Cost estimate — token counts × public per-model pricing → US$ equivalent.
  6. Per-model     — token + cost breakdown split by model id.

Everything is local and read-only except the small history DB this module
owns. No new third-party dependency.
"""

from __future__ import annotations

import glob
import json
import os
import sqlite3
import threading
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

# --- Paths ------------------------------------------------------------------
CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
DB_PATH = CLAUDE_DIR / "usage_tray_history.db"

# --- Pricing (USD per 1M tokens, public list prices) ------------------------
# input / output / cache_read / cache_write(5m). Matched by substring on the
# model id (longest match wins). Update when prices change.
_PRICING = {
    "opus":   {"in": 15.0, "out": 75.0, "cache_read": 1.50, "cache_write": 18.75},
    "sonnet": {"in": 3.0,  "out": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "haiku":  {"in": 0.80, "out": 4.0,  "cache_read": 0.08, "cache_write": 1.00},
}
_DEFAULT_PRICE = _PRICING["sonnet"]


def _price_for(model: str) -> dict:
    model = (model or "").lower()
    for key, price in _PRICING.items():
        if key in model:
            return price
    return _DEFAULT_PRICE


def _short_model(model: str) -> str:
    m = (model or "?").lower()
    for key in _PRICING:
        if key in m:
            return key.capitalize()
    return model or "?"


# =====================================================================
# 1. History (SQLite)
# =====================================================================
_db_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS history (
               ts          TEXT NOT NULL,
               five_hour   REAL,
               seven_day   REAL,
               seven_sonnet REAL
           )"""
    )
    return conn


def record_history(usage: dict) -> None:
    """Append one row with the current utilization of each window."""
    def pct(key: str):
        win = usage.get(key)
        return None if not win else float(win.get("utilization", 0))

    row = (
        datetime.now(timezone.utc).isoformat(),
        pct("five_hour"),
        pct("seven_day"),
        pct("seven_day_sonnet"),
    )
    with _db_lock, _connect() as conn:
        conn.execute(
            "INSERT INTO history (ts, five_hour, seven_day, seven_sonnet) "
            "VALUES (?, ?, ?, ?)",
            row,
        )


def history_rows(window: str, since_hours: float) -> list[tuple[datetime, float]]:
    """Return [(ts, pct)] for one column, newest within since_hours."""
    col = {
        "five_hour": "five_hour",
        "seven_day": "seven_day",
        "seven_day_sonnet": "seven_sonnet",
    }[window]
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
    with _db_lock, _connect() as conn:
        cur = conn.execute(
            f"SELECT ts, {col} FROM history WHERE ts >= ? AND {col} IS NOT NULL "
            "ORDER BY ts",
            (cutoff,),
        )
        out = []
        for ts, val in cur.fetchall():
            try:
                out.append((datetime.fromisoformat(ts), float(val)))
            except (ValueError, TypeError):
                continue
        return out


def trend_arrow(window: str) -> str:
    """'↑ +4%/h', '↓ -2%/h' or '' — slope over last ~3h of history."""
    rows = history_rows(window, since_hours=3)
    if len(rows) < 2:
        return ""
    (t0, v0), (t1, v1) = rows[0], rows[-1]
    hours = (t1 - t0).total_seconds() / 3600
    if hours <= 0:
        return ""
    rate = (v1 - v0) / hours
    if abs(rate) < 0.5:
        return "→ estável"
    arrow = "↑" if rate > 0 else "↓"
    return f"{arrow} {rate:+.0f}%/h"


# =====================================================================
# 2. Burn-rate projection
# =====================================================================
def project_full(window: str) -> str:
    """Estimate when the window hits 100% at the recent rate. '' if unknown."""
    rows = history_rows(window, since_hours=6)
    if len(rows) < 2:
        return ""
    (t0, v0), (t1, v1) = rows[0], rows[-1]
    hours = (t1 - t0).total_seconds() / 3600
    if hours <= 0:
        return ""
    rate = (v1 - v0) / hours  # %/hour
    if rate <= 0.5 or v1 >= 100:
        return ""
    hours_left = (100 - v1) / rate
    eta = datetime.now().astimezone() + timedelta(hours=hours_left)
    if hours_left < 1:
        when = f"~{round(hours_left * 60)}min"
    elif hours_left < 48:
        when = f"~{hours_left:.1f}h"
    else:
        when = f"~{hours_left / 24:.1f}d"
    return f"atinge 100% em {when} ({eta:%d/%m %H:%M})"


# =====================================================================
# 3. Real token counts from session logs
# =====================================================================
def _iter_assistant_usages(since: datetime):
    """Yield (ts, model, usage_dict) for assistant messages newer than `since`."""
    if not PROJECTS_DIR.exists():
        return
    pattern = os.path.join(str(PROJECTS_DIR), "**", "*.jsonl")
    for fp in glob.glob(pattern, recursive=True):
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(fp), timezone.utc)
        except OSError:
            continue
        if mtime < since:
            continue  # whole file older than window — skip
        try:
            with open(fp, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if obj.get("type") != "assistant":
                        continue
                    raw_ts = obj.get("timestamp")
                    if not raw_ts:
                        continue
                    try:
                        ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if ts < since:
                        continue
                    msg = obj.get("message", {})
                    usage = msg.get("usage")
                    if not usage:
                        continue
                    # message.id (fallback requestId) identifies a unique API
                    # response; the same one is logged many times across
                    # streaming/tool turns and resumed sessions — caller dedupes.
                    msg_id = msg.get("id") or obj.get("requestId")
                    yield msg_id, ts, msg.get("model", "?"), usage
        except OSError:
            continue


def _blank_bucket() -> dict:
    return {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0}


def _add_usage(bucket: dict, model: str, usage: dict) -> None:
    i = int(usage.get("input_tokens", 0))
    o = int(usage.get("output_tokens", 0))
    cr = int(usage.get("cache_read_input_tokens", 0))
    cw = int(usage.get("cache_creation_input_tokens", 0))
    bucket["in"] += i
    bucket["out"] += o
    bucket["cache_read"] += cr
    bucket["cache_write"] += cw
    p = _price_for(model)
    bucket["cost"] += (
        i * p["in"] + o * p["out"] + cr * p["cache_read"] + cw * p["cache_write"]
    ) / 1_000_000


def token_report(since_hours: float = 24) -> dict:
    """Aggregate real token spend over the last `since_hours`.

    Returns {total, by_model: {name: bucket}, by_project: {name: bucket}}.
    Each bucket has in/out/cache_read/cache_write token counts + cost (USD).
    """
    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    # A single API response is logged several times (streaming partials, tool
    # turns, resumed sessions). Most dupes are identical, but streaming partials
    # share the message.id with a lower output_tokens than the final row. Keep
    # the row with the highest output_tokens per id = the completed response.
    best: dict = {}
    n = 0
    for msg_id, ts, model, usage in _iter_assistant_usages(since):
        n += 1
        key = msg_id if msg_id is not None else f"__noid_{n}"
        out = int(usage.get("output_tokens", 0))
        prev = best.get(key)
        if prev is None or out > prev[1]:
            best[key] = (model, out, usage)

    total = _blank_bucket()
    by_model: dict[str, dict] = defaultdict(_blank_bucket)
    for model, _out, usage in best.values():
        name = _short_model(model)
        _add_usage(total, model, usage)
        _add_usage(by_model[name], model, usage)
    return {"total": total, "by_model": dict(by_model)}


# =====================================================================
# 5/6. Formatting helpers for the detail window
# =====================================================================
def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def token_block(since_hours: float = 24) -> list[str]:
    """Pretty multi-line block for the detail window. [] if no data."""
    rep = token_report(since_hours)
    t = rep["total"]
    cache = t["cache_read"] + t["cache_write"]
    if t["in"] + t["out"] + cache == 0:
        return []
    label = f"{since_hours / 24:.0f}d" if since_hours >= 24 else f"{since_hours:.0f}h"
    out = [f"▶ Tokens reais (últimas {label}, dos logs do Claude Code)"]
    # Headline = real I/O (input+output). These are what you actually
    # "wrote/read"; cache is shown apart because cache reads are huge but
    # cheap and would otherwise dominate the number.
    out.append(
        f"   Entrada+saída: {_fmt_tokens(t['in'] + t['out'])}  "
        f"(in {_fmt_tokens(t['in'])} · out {_fmt_tokens(t['out'])})"
    )
    out.append(
        f"   Cache: {_fmt_tokens(cache)}  "
        f"(leitura {_fmt_tokens(t['cache_read'])} · escrita {_fmt_tokens(t['cache_write'])})"
    )
    for name, b in sorted(
        rep["by_model"].items(), key=lambda kv: kv[1]["cost"], reverse=True
    ):
        io = b["in"] + b["out"]
        bcache = b["cache_read"] + b["cache_write"]
        if io + bcache == 0:
            continue
        out.append(
            f"     • {name}: {_fmt_tokens(io)} in+out · {_fmt_tokens(bcache)} cache"
        )
    out.append(
        f"   Se fosse pago por API (preço público): ~US$ {t['cost']:.2f}. "
        "No Enterprise não é cobrado — só referência de volume."
    )
    return out


# =====================================================================
# 4. Threshold notifications — state tracking
# =====================================================================
_THRESHOLDS = (70, 90, 100)
_last_band: dict[str, int] = {}


def threshold_crossings(usage: dict) -> list[tuple[str, int]]:
    """Return [(window_label, threshold)] newly crossed upward since last call."""
    crossings = []
    windows = {
        "five_hour": "5h",
        "seven_day": "7d",
        "seven_day_sonnet": "7d Sonnet",
    }
    for key, label in windows.items():
        win = usage.get(key)
        if not win:
            continue
        pct = float(win.get("utilization", 0))
        band = max((t for t in _THRESHOLDS if pct >= t), default=0)
        prev = _last_band.get(key, 0)
        if band > prev:
            crossings.append((label, band))
        _last_band[key] = band
    return crossings
