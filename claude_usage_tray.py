"""Claude usage tray widget for Windows.

A system-tray equivalent of ai-usagebar's Waybar widget. Reads the OAuth
credentials that Claude Code stores in ~/.claude/.credentials.json, hits the
undocumented usage endpoint, and shows the current 5-hour / 7-day utilization
as a colored tray icon. Hover for a short tooltip; left-click (or the
"Detalhes" menu item) opens a window explaining every limit in detail.
Refreshes the token automatically when it is about to expire (same flow as
src/anthropic/oauth.rs in this repo).

Requirements:
    pip install pystray pillow requests

Run:
    python claude_usage_tray.py
"""

from __future__ import annotations

import json
import threading
import time
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont
from pystray import Icon, Menu, MenuItem

import usage_extras

# --- Constants mirrored from this repo (src/anthropic/{fetch,oauth}.rs) -----
CREDS_PATH = Path.home() / ".claude" / ".credentials.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # public Claude CLI id
BETA_HEADER = "oauth-2025-04-20"
USER_AGENT = "claude-cli/1.0"
REFRESH_BUFFER_SECS = 300
POLL_INTERVAL_SECS = 300  # how often we re-fetch usage

# Latest fetch result, shared between the poll loop and the detail window.
_state: dict = {"usage": None, "plan": "?", "error": None, "fetched_at": None}
_state_lock = threading.Lock()

# Set by "Atualizar agora" to wake the poll loop immediately.
_refresh_now = threading.Event()


# --- Credentials + token refresh -------------------------------------------
def read_creds() -> dict:
    return json.loads(CREDS_PATH.read_text(encoding="utf-8"))["claudeAiOauth"]


def write_creds(oauth: dict) -> None:
    """Persist a refreshed oauth blob, preserving other top-level fields."""
    try:
        doc = json.loads(CREDS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    doc["claudeAiOauth"] = oauth
    CREDS_PATH.write_text(json.dumps(doc, indent=2), encoding="utf-8")


def plan_label(oauth: dict) -> str:
    """Render the plan name the way ai-usagebar does (creds.rs:69)."""
    name = (oauth.get("subscriptionType") or "").capitalize() or "Unknown"
    tier = oauth.get("rateLimitTier", "")
    if "5x" in tier:
        name += " 5x"
    elif "20x" in tier:
        name += " 20x"
    return name


def maybe_refresh(oauth: dict) -> dict:
    """Refresh the access token if it expires within the buffer window."""
    expires_at_secs = int(oauth.get("expiresAt", 0)) / 1000
    if expires_at_secs >= time.time() + REFRESH_BUFFER_SECS:
        return oauth  # still valid

    resp = requests.post(
        TOKEN_URL,
        headers={
            "Content-Type": "application/json",
            "anthropic-beta": BETA_HEADER,
            "User-Agent": USER_AGENT,
        },
        json={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": oauth["refreshToken"],
        },
        timeout=25,
    )
    resp.raise_for_status()
    data = resp.json()
    oauth["accessToken"] = data["access_token"]
    if data.get("refresh_token"):
        oauth["refreshToken"] = data["refresh_token"]
    oauth["expiresAt"] = int((time.time() + data["expires_in"]) * 1000)
    write_creds(oauth)
    return oauth


def fetch_usage() -> tuple[dict, str]:
    oauth = maybe_refresh(read_creds())
    resp = requests.get(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {oauth['accessToken']}",
            "anthropic-beta": BETA_HEADER,
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json(), plan_label(oauth)


# --- Formatting helpers -----------------------------------------------------
def pct_of(usage: dict, key: str) -> int | None:
    win = usage.get(key)
    if not win:
        return None
    return round(float(win.get("utilization", 0)))


def financial_pct(usage: dict) -> int | None:
    """Spend utilization (%) from the financial cap. Used by Enterprise/PAYG
    plans where the percentage windows come back null and the real limit is a
    monthly US$ budget (extra_usage)."""
    extra = usage.get("extra_usage")
    if not extra or not extra.get("is_enabled"):
        return None
    util = extra.get("utilization")
    if util is None:
        return None
    return round(float(util))


def overall_pct(usage: dict) -> int:
    """Highest utilization across every signal available, financial included.
    Enterprise has null %-windows, so without the financial fallback the icon
    would sit at 0 forever."""
    vals = [
        pct_of(usage, "five_hour"),
        pct_of(usage, "seven_day"),
        pct_of(usage, "seven_day_sonnet"),
        financial_pct(usage),
    ]
    return max((v for v in vals if v is not None), default=0)


def humanize_reset(usage: dict, key: str) -> str:
    """Return 'em 3h 12min (14:30)' or '' if no reset timestamp."""
    win = usage.get(key) or {}
    raw = win.get("resets_at")
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return ""
    local = dt.astimezone()
    delta = dt - datetime.now(timezone.utc)
    secs = int(delta.total_seconds())
    if secs <= 0:
        when = "agora"
    else:
        h, m = divmod(secs // 60, 60)
        d, h = divmod(h, 24)
        parts = []
        if d:
            parts.append(f"{d}d")
        if h:
            parts.append(f"{h}h")
        if not d:
            parts.append(f"{m}min")
        when = "em " + " ".join(parts)
    return f"{when} ({local:%d/%m %H:%M})"


def advice(pct: int) -> str:
    if pct >= 90:
        return "⚠ quase no limite — considere pausar ou trocar de modelo"
    if pct >= 70:
        return "atenção — uso alto"
    return "tranquilo"


# --- Tray icon rendering ----------------------------------------------------
def color_for(pct: int) -> tuple[int, int, int]:
    if pct >= 90:
        return (220, 50, 50)      # red
    if pct >= 70:
        return (230, 160, 40)     # amber
    return (60, 180, 90)          # green


def make_icon_image(pct: int) -> Image.Image:
    img = Image.new("RGB", (64, 64), color_for(pct))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 30)
    except OSError:
        font = ImageFont.load_default()
    text = str(min(pct, 99))
    bbox = draw.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((64 - w) / 2 - bbox[0], (64 - h) / 2 - bbox[1]),
              text, fill="white", font=font)
    return img


def short_tooltip(usage: dict, plan: str) -> str:
    """Concise tooltip — Windows caps the tray tooltip at ~128 chars."""
    h5 = pct_of(usage, "five_hour")
    d7 = pct_of(usage, "seven_day")
    lines = [f"Claude — {plan}"]
    if h5 is not None:
        lines.append(f"5h:  {h5}%")
    if d7 is not None:
        lines.append(f"7d:  {d7}%")
    extra = usage.get("extra_usage")
    if extra and extra.get("is_enabled"):
        cur = extra.get("currency", "USD")
        used = extra.get("used_credits", 0) / 100
        limit = extra.get("monthly_limit", 0) / 100
        fp = financial_pct(usage)
        lines.append(f"Gasto: {cur} {used:.2f}/{limit:.2f} ({fp}%)")
    lines.append("(clique para detalhes)")
    return "\n".join(lines)


# --- Detail window (tkinter) ------------------------------------------------
def build_detail_text() -> str:
    with _state_lock:
        usage = _state["usage"]
        plan = _state["plan"]
        err = _state["error"]
        fetched_at = _state["fetched_at"]

    if err:
        return f"Erro ao buscar uso:\n\n{err}\n\nVerifique sua conexão ou rode `claude` para reautenticar."
    if not usage:
        return "Carregando dados de uso…"

    out = [f"Plano: {plan}", ""]

    blocks = [
        ("Janela de 5 horas", "five_hour",
         "Limite de curto prazo. Reseta a cada 5 horas a partir do primeiro uso da janela."),
        ("Janela de 7 dias (todos os modelos)", "seven_day",
         "Limite semanal geral. É o que costuma travar o uso por mais tempo."),
        ("Janela de 7 dias (somente Sonnet)", "seven_day_sonnet",
         "Limite semanal específico para o modelo Sonnet."),
    ]
    for title, key, explain in blocks:
        pct = pct_of(usage, key)
        if pct is None:
            continue
        bar_filled = round(pct / 10)
        bar = "█" * bar_filled + "░" * (10 - bar_filled)
        reset = humanize_reset(usage, key)
        out.append(f"▶ {title}")
        out.append(f"   {bar}  {pct}%  —  {advice(pct)}")
        trend = usage_extras.trend_arrow(key)
        proj = usage_extras.project_full(key)
        if trend:
            line = f"   Tendência: {trend}"
            if proj:
                line += f"  ·  {proj}"
            out.append(line)
        if reset:
            out.append(f"   Reseta {reset}")
        out.append(f"   {explain}")
        out.append("")

    extra = usage.get("extra_usage")
    if extra and extra.get("is_enabled"):
        limit = extra.get("monthly_limit", 0) / 100
        used = extra.get("used_credits", 0) / 100
        out.append("▶ Créditos extras (pay-as-you-go)")
        out.append(f"   Usado: US$ {used:.2f} de US$ {limit:.2f}")
        out.append("   Cobrança avulsa quando os limites do plano se esgotam.")
        out.append("")

    try:
        tok = usage_extras.token_block(since_hours=24)
        if tok:
            out.extend(tok)
            out.append("")
    except Exception as exc:  # log parsing must never break the window
        out.append(f"(tokens reais indisponíveis: {exc})")
        out.append("")

    if fetched_at:
        out.append(f"Atualizado: {fetched_at:%d/%m %H:%M:%S}  (atualiza a cada {POLL_INTERVAL_SECS // 60} min)")
    return "\n".join(out)


def _show_text_window(title: str, body: str, geometry: str = "520x460") -> None:
    """Open a read-only dark text window in its own Tk mainloop (own thread)."""
    def run() -> None:
        root = tk.Tk()
        root.title(title)
        root.geometry(geometry)
        root.configure(bg="#1e1e2e")
        text = tk.Text(
            root, wrap="word", bg="#1e1e2e", fg="#e0e0e0",
            font=("Consolas", 11), borderwidth=0, padx=16, pady=14,
        )
        text.insert("1.0", body)
        text.configure(state="disabled")
        text.pack(fill="both", expand=True)
        root.attributes("-topmost", True)
        root.mainloop()

    threading.Thread(target=run, daemon=True).start()


def show_detail_window() -> None:
    _show_text_window("Limites de uso — Claude", build_detail_text())


def build_token_text() -> str:
    """7-day real-token breakdown from the Claude Code session logs."""
    try:
        block = usage_extras.token_block(since_hours=24 * 7)
    except Exception as exc:
        return f"Erro ao ler logs de sessão:\n\n{exc}"
    if not block:
        return ("Nenhum uso de token encontrado nos últimos 7 dias.\n\n"
                "Os dados vêm de ~/.claude/projects/*.jsonl (logs do Claude Code).")
    return "\n".join(block)


def show_token_window() -> None:
    _show_text_window("Tokens reais (7 dias) — Claude", build_token_text())


# --- Poll loop --------------------------------------------------------------
def update_loop(icon: Icon) -> None:
    while True:
        try:
            usage, plan = fetch_usage()
            with _state_lock:
                _state.update(usage=usage, plan=plan, error=None,
                              fetched_at=datetime.now())
            try:
                usage_extras.record_history(usage)
                for label, band in usage_extras.threshold_crossings(usage):
                    icon.notify(
                        f"{label} chegou a {band}% do limite.",
                        "Claude — uso alto",
                    )
            except Exception:
                pass  # analytics must never crash the tray
            icon.icon = make_icon_image(overall_pct(usage))
            icon.title = short_tooltip(usage, plan)
        except Exception as exc:  # never crash the tray; surface the error
            with _state_lock:
                _state.update(error=str(exc), fetched_at=datetime.now())
            icon.icon = make_icon_image(0)
            icon.title = f"Claude — erro\n{exc}"
        # Wake early if "Atualizar agora" was clicked; else poll on schedule.
        _refresh_now.wait(POLL_INTERVAL_SECS)
        _refresh_now.clear()


def main() -> None:
    icon = Icon(
        "claude-usage",
        make_icon_image(0),
        "Claude usage — carregando…",
        menu=Menu(
            MenuItem("Detalhes dos limites", lambda i: show_detail_window(),
                     default=True),
            MenuItem("Tokens reais (7 dias)", lambda i: show_token_window()),
            MenuItem("Atualizar agora", lambda i: _refresh_now.set()),
            Menu.SEPARATOR,
            MenuItem("Sair", lambda i: i.stop()),
        ),
    )
    threading.Thread(target=update_loop, args=(icon,), daemon=True).start()
    icon.run()


if __name__ == "__main__":
    main()
