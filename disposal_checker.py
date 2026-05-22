"""
Public TWSE / TPEx disposal stock lookup.

This module intentionally uses only public exchange endpoints so the disposal
page can run as a standalone tool without broker login or private data tokens.
"""

import re
from datetime import datetime, date as date_type

import requests
import urllib3


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_CACHE = None
_CACHE_DATE = None


def clear_cache():
    """Clear the daily public exchange-data cache."""
    global _CACHE, _CACHE_DATE
    _CACHE = None
    _CACHE_DATE = None


def _roc_to_ad(d: str) -> str:
    """Convert ROC date strings such as 115/04/16 to 2026/04/16."""
    if not d:
        return ""
    s = str(d).strip()
    return re.sub(
        r"(?<!\d)(\d{2,3})([/-])(\d{1,2})([/-])(\d{1,2})(?!\d)",
        lambda m: f"{int(m.group(1)) + 1911}/{int(m.group(3)):02d}/{int(m.group(5)):02d}",
        s,
    )


def _convert_period(period_str: str) -> str:
    """Normalize a disposal-period string to western-year dates when possible."""
    if not period_str:
        return ""
    return _roc_to_ad(str(period_str).replace("至", " ~ ").replace("-", " ~ "))


def _is_period_active(period_ad: str) -> bool:
    """Return True if the period contains today, or if it cannot be parsed safely."""
    if not period_ad:
        return True
    matches = re.findall(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", period_ad)
    if not matches:
        return True
    try:
        dates = [date_type(int(y), int(m), int(d)) for y, m, d in matches]
        today = date_type.today()
        if len(dates) >= 2:
            return min(dates) <= today <= max(dates)
        return today <= dates[0]
    except Exception:
        return True


def _strip_html(value) -> str:
    text = re.sub(r"<[^>]+>", "", str(value or ""))
    return text.replace("&nbsp;", " ").strip()


def _short_measure(text: str) -> str:
    text = _strip_html(text)
    match = re.search(r"(每\d+分鐘撮合一次)", text)
    if match:
        return f"預收款券、人工管制撮合({match.group(1)})"
    return text[:120] if text else ""


def _fetch_twse(hdrs: dict) -> dict:
    disposed = {}
    url = "https://www.twse.com.tw/rwd/zh/announcement/punish?response=json"
    resp = requests.get(url, headers=hdrs, timeout=10, verify=False)
    resp.raise_for_status()
    data = resp.json()

    fields = data.get("fields") or []

    def field_index(name, fallback):
        for i, field in enumerate(fields):
            if name in str(field):
                return i
        return fallback

    idx_id = field_index("代號", 2)
    idx_name = field_index("名稱", 3)
    idx_cond = field_index("條件", 5)
    idx_period = field_index("起迄", 6)
    idx_measure = field_index("措施", 7)

    for row in data.get("data") or []:
        try:
            if len(row) <= idx_id:
                continue
            stock_id = _strip_html(row[idx_id])
            if not stock_id[:4].isdigit():
                continue
            period = _convert_period(_strip_html(row[idx_period] if len(row) > idx_period else ""))
            if not _is_period_active(period):
                continue
            disposed[stock_id] = {
                "name": _strip_html(row[idx_name] if len(row) > idx_name else ""),
                "period": period,
                "condition": _strip_html(row[idx_cond] if len(row) > idx_cond else ""),
                "measures": _short_measure(row[idx_measure] if len(row) > idx_measure else ""),
                "source": "TWSE",
            }
        except Exception as row_e:
            print(f"[TWSE] row parse failed: {row_e} row={row}")
    return disposed


def _fetch_tpex(hdrs: dict) -> dict:
    disposed = {}
    tpex_hdrs = {
        **hdrs,
        "Referer": "https://www.tpex.org.tw/zh-tw/announce/market/disposal.html",
        "X-Requested-With": "XMLHttpRequest",
    }
    url = "https://www.tpex.org.tw/www/zh-tw/bulletin/disposal?response=json"
    resp = requests.get(url, headers=tpex_hdrs, timeout=10, verify=False)
    resp.raise_for_status()
    data = resp.json()
    rows = data.get("tables", [{}])[0].get("data", []) if isinstance(data, dict) else data

    for row in rows:
        try:
            if not isinstance(row, (list, tuple)) or len(row) < 8:
                continue
            stock_id = _strip_html(row[2])
            if not stock_id[:4].isdigit():
                continue
            name_raw = _strip_html(row[3])
            period = _convert_period(_strip_html(row[5]))
            if not _is_period_active(period):
                continue
            disposed[stock_id] = {
                "name": name_raw.split("(")[0].strip(),
                "period": period,
                "condition": re.sub(r"\(.*?\)", "", _strip_html(row[6]))[:80],
                "measures": _short_measure(row[7]),
                "source": "TPEx",
            }
        except Exception as row_e:
            print(f"[TPEx] row parse failed: {row_e} row={row}")
    return disposed


def fetch_disposed_stocks(refresh: bool = False) -> dict:
    """
    Fetch current disposal stocks from public TWSE / TPEx endpoints.

    Returns {"disposed": {stock_id: {...}}, "fetch_time": "..."}.
    """
    global _CACHE, _CACHE_DATE
    today = date_type.today().isoformat()
    if not refresh and _CACHE is not None and _CACHE_DATE == today:
        return _CACHE

    hdrs = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
    }
    disposed = {}
    errors = {}

    try:
        disposed.update(_fetch_twse(hdrs))
    except Exception as e:
        errors["twse"] = str(e)
        print(f"TWSE disposal fetch failed: {e}")

    try:
        disposed.update(_fetch_tpex(hdrs))
    except Exception as e:
        errors["tpex"] = str(e)
        print(f"TPEx disposal fetch failed: {e}")

    result = {
        "disposed": dict(sorted(disposed.items())),
        "fetch_time": datetime.now().isoformat(timespec="seconds"),
        "sources": ["TWSE", "TPEx"],
    }
    if errors:
        result["errors"] = errors

    _CACHE = result
    _CACHE_DATE = today
    return result


def check_single_stock(stock_id: str) -> dict:
    """Return current public disposal status for one stock."""
    sid = str(stock_id).strip()
    data = fetch_disposed_stocks()
    info = data.get("disposed", {}).get(sid)
    return {
        "stock_id": sid,
        "is_disposed": bool(info),
        "info": info,
        "fetch_time": data.get("fetch_time"),
        "sources": data.get("sources", []),
    }


def check_all_conditions() -> dict:
    """Compatibility endpoint for older callers; public standalone mode has no scan."""
    data = fetch_disposed_stocks()
    return {
        "scan_date": date_type.today().isoformat(),
        "total_flagged": len(data.get("disposed", {})),
        "by_condition": {},
        "stocks": [
            {
                "stock_id": sid,
                "name": info.get("name", ""),
                "conditions": [],
                "count": 0,
                "source": info.get("source", ""),
                "period": info.get("period", ""),
            }
            for sid, info in data.get("disposed", {}).items()
        ],
        "public_only": True,
        "message": "Public standalone mode provides the current exchange disposal list only.",
    }
