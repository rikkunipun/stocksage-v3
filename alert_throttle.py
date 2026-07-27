import json
import os
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

DATA_DIR     = os.path.join(os.path.dirname(__file__), "data")
STATE_FILE   = os.path.join(DATA_DIR, "throttle_state.json")
IST          = ZoneInfo("Asia/Kolkata")

HOURLY_LIMIT = 3
DAILY_LIMIT  = 10
MAX_SEEN     = 50
MIN_SEVERITY = 60
SIMILARITY_THRESHOLD = 0.80

STOP_WORDS = {"the", "a", "an", "is", "are", "was", "in", "on", "at",
              "to", "of", "and", "for", "by", "with", "as", "it", "its"}

_DEFAULT_STATE = {
    "hourly_count":    0,
    "daily_count":     0,
    "mute_until":      None,   # ISO string or null
    "last_reset_hour": None,
    "last_reset_date": None,
    "seen_headlines":  [],
}


# ── State persistence ──────────────────────────────────────────────────────────

def _now_ist() -> datetime:
    return datetime.now(IST)


def _maybe_reset(state: dict) -> bool:
    """
    Lazy time-based resets: no external scheduler required.
    Resets hourly_count when the wall-clock hour has rolled over since the
    last reset, and daily_count + seen_headlines when the date has.
    Returns True if the state was modified.
    """
    changed = False
    now = _now_ist()

    last_hour = state.get("last_reset_hour")
    try:
        last_hour_dt = datetime.fromisoformat(last_hour) if last_hour else None
    except (ValueError, TypeError):
        last_hour_dt = None
    hour_floor = now.replace(minute=0, second=0, microsecond=0)
    if last_hour_dt is None or last_hour_dt < hour_floor:
        state["hourly_count"] = 0
        state["last_reset_hour"] = now.isoformat()
        changed = True

    if state.get("last_reset_date") != now.date().isoformat():
        state["daily_count"] = 0
        state["seen_headlines"] = []
        state["last_reset_date"] = now.date().isoformat()
        changed = True

    return changed


def _load_state() -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(STATE_FILE):
        state = dict(_DEFAULT_STATE)
    else:
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
            # Fill any missing keys from defaults
            for k, v in _DEFAULT_STATE.items():
                state.setdefault(k, v)
        except Exception as e:
            print(f"WARNING: Could not load throttle_state.json — {e}. Using defaults.")
            state = dict(_DEFAULT_STATE)

    if _maybe_reset(state):
        _save_state(state)
    return state


def _save_state(state: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=DATA_DIR, delete=False, suffix=".tmp"
    ) as tmp:
        json.dump(state, tmp, indent=2)
        tmp_path = tmp.name
    os.replace(tmp_path, STATE_FILE)


# ── Deduplication ──────────────────────────────────────────────────────────────

def _tokenize(headline: str) -> set:
    words = headline.lower().split()
    return {w.strip(".,!?\"'") for w in words if w not in STOP_WORDS and len(w) > 1}


def _is_duplicate(headline: str, seen: list) -> bool:
    tokens = _tokenize(headline)
    if not tokens:
        return False
    for past in seen:
        past_tokens = _tokenize(past)
        if not past_tokens:
            continue
        overlap = len(tokens & past_tokens) / max(len(tokens), len(past_tokens))
        if overlap >= SIMILARITY_THRESHOLD:
            return True
    return False


# ── Public API ─────────────────────────────────────────────────────────────────

def is_seen(headline: str) -> bool:
    """True if headline is a (near-)duplicate of one already alerted on today."""
    state = _load_state()
    return _is_duplicate(headline, state["seen_headlines"])


def can_send_alert(headline: str, severity_score: int) -> bool:
    """
    Return True only if ALL conditions pass:
    - severity_score >= 60
    - hourly_count < 3
    - daily_count < 10
    - not currently muted
    - headline not a duplicate of a recently seen one
    """
    if severity_score < MIN_SEVERITY:
        return False

    state = _load_state()

    if is_muted():
        return False

    if state["hourly_count"] >= HOURLY_LIMIT:
        return False

    if state["daily_count"] >= DAILY_LIMIT:
        return False

    if _is_duplicate(headline, state["seen_headlines"]):
        return False

    return True


def record_alert_sent(headline: str):
    """Increment counters and add headline to seen list. Saves state atomically."""
    state = _load_state()
    state["hourly_count"] += 1
    state["daily_count"]  += 1
    state["seen_headlines"].append(headline)
    state["seen_headlines"] = state["seen_headlines"][-MAX_SEEN:]
    _save_state(state)


def set_mute(hours: int) -> str:
    state = _load_state()
    until = _now_ist() + timedelta(hours=hours)
    state["mute_until"] = until.isoformat()
    _save_state(state)
    return f"🔇 Geo-alerts muted for {hours} hour(s). Resumes at {until.strftime('%H:%M IST')}."


def clear_mute() -> str:
    state = _load_state()
    state["mute_until"] = None
    _save_state(state)
    return "🔔 Geo-alerts resumed."


def is_muted() -> bool:
    state = _load_state()
    mute_until = state.get("mute_until")
    if not mute_until:
        return False
    try:
        until_dt = datetime.fromisoformat(mute_until)
        return _now_ist() < until_dt
    except (ValueError, TypeError):
        return False


def reset_hourly_count():
    """Reset hourly alert counter — called every hour by scheduler."""
    state = _load_state()
    state["hourly_count"]    = 0
    state["last_reset_hour"] = _now_ist().isoformat()
    _save_state(state)
    print(f"[throttle] Hourly count reset at {_now_ist().strftime('%H:%M IST')}")


def reset_daily_count():
    """Reset daily counter and clear seen headlines — called at midnight by scheduler."""
    state = _load_state()
    state["daily_count"]     = 0
    state["seen_headlines"]  = []
    state["last_reset_date"] = _now_ist().date().isoformat()
    _save_state(state)
    print(f"[throttle] Daily count + seen headlines reset at {_now_ist().strftime('%d %b %Y %H:%M IST')}")


def get_status() -> dict:
    """Return current throttle state for /status command."""
    state = _load_state()
    muted = is_muted()
    mute_until_str = ""
    if muted and state.get("mute_until"):
        try:
            until_dt = datetime.fromisoformat(state["mute_until"])
            mute_until_str = until_dt.strftime("%H:%M IST")
        except (ValueError, TypeError):
            pass

    return {
        "hourly_remaining": max(0, HOURLY_LIMIT - state["hourly_count"]),
        "daily_remaining":  max(0, DAILY_LIMIT  - state["daily_count"]),
        "mute_status":      f"Active until {mute_until_str}" if muted else "Inactive",
        "mute_until":       mute_until_str,
        "seen_count":       len(state["seen_headlines"]),
    }


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    # Reset state for clean test
    _save_state(dict(_DEFAULT_STATE))
    print("=== Alert Throttle Simulation ===\n")

    test_cases = [
        ("RBI cuts rates by 50bps — market rally expected",                   85),
        ("RBI cuts rates by 50 basis points — surprise move for markets",     85),  # near-duplicate
        ("West Asia conflict escalates: oil supply disruption fears grow",     75),
        ("Local weather forecast for Mumbai this weekend",                     20),  # low severity
        ("Nifty crashes 800 points on US tariff shock, FIIs sell ₹12,000cr", 92),
        ("Global oil crisis deepens: OPEC cuts output amid West Asia war",    70),
    ]

    for headline, severity in test_cases:
        allowed = can_send_alert(headline, severity)
        status  = "✅ SEND" if allowed else "🚫 BLOCK"
        reason  = ""
        if not allowed:
            if severity < MIN_SEVERITY:
                reason = "(low severity)"
            elif is_muted():
                reason = "(muted)"
            else:
                state = _load_state()
                if _is_duplicate(headline, state["seen_headlines"]):
                    reason = "(duplicate)"
                elif state["hourly_count"] >= HOURLY_LIMIT:
                    reason = "(hourly limit)"
                elif state["daily_count"] >= DAILY_LIMIT:
                    reason = "(daily limit)"

        print(f"{status} [{severity:3d}] {headline[:65]:<65} {reason}")
        if allowed:
            record_alert_sent(headline)

    print(f"\n--- Mute test ---")
    print(set_mute(2))
    print(f"is_muted() = {is_muted()}")
    print(f"can_send_alert('Breaking news', 90) = {can_send_alert('Breaking news', 90)}")
    print(clear_mute())
    print(f"is_muted() = {is_muted()}")

    print(f"\n--- Final throttle state ---")
    status = get_status()
    for k, v in status.items():
        print(f"  {k:<22}: {v}")


if __name__ == "__main__":
    main()
