"""
Auto-fetch currency exchange rates from CBBiH (Central Bank of Bosnia & Herzegovina).
API: https://www.cbbh.ba/CurrencyExchange/GetJson
Free, no auth required. Returns daily rates vs BAM.
"""
import logging
from datetime import date, datetime
from functools import lru_cache

logger = logging.getLogger("currency_api")

_CBBH_URL = "https://www.cbbh.ba/CurrencyExchange/GetJson"

# Simple in-memory cache: {(currency, date_str): rate}
_rate_cache: dict[tuple, float] = {}


def fetch_rate(currency: str = "EUR") -> float:
    """
    Fetch today's BAM exchange rate for the given foreign currency.
    Returns 0.0 on failure (caller should handle gracefully).
    CBBiH rate format: 1 unit of foreign currency = X BAM.
    """
    currency = currency.upper().strip()
    today = date.today().isoformat()
    cache_key = (currency, today)

    if cache_key in _rate_cache:
        return _rate_cache[cache_key]

    try:
        import urllib.request, json as _json
        url = f"{_CBBH_URL}?date={today}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = _json.loads(resp.read().decode())
    except Exception as e:
        logger.warning(f"CBBiH API greška za {currency}: {e}")
        return _fallback_rate(currency)

    # CBBiH JSON structure: list of {AlphaCode, Units, BuyingRate, MiddleRate, SellingRate}
    items = data if isinstance(data, list) else data.get("CurrencyExchangeItems", [])
    for item in items:
        code = (item.get("AlphaCode") or item.get("currencyCode") or "").upper()
        if code == currency:
            rate_str = item.get("MiddleRate") or item.get("middleRate") or "0"
            units_str = item.get("Units") or item.get("units") or "1"
            try:
                rate = float(str(rate_str).replace(",", "."))
                units = float(str(units_str).replace(",", "."))
                if units and rate:
                    # CBBiH gives rate per `units` of currency → normalise to per 1 unit
                    per_unit = round(rate / units, 6)
                    _rate_cache[cache_key] = per_unit
                    logger.info(f"CBBiH kurs {currency}/BAM: {per_unit} (datum: {today})")
                    return per_unit
            except (ValueError, ZeroDivisionError):
                pass

    logger.warning(f"CBBiH: kurs za {currency} nije pronađen, koristim fallback")
    return _fallback_rate(currency)


def _fallback_rate(currency: str) -> float:
    """Static fallback rates when API is unavailable."""
    fallbacks = {
        "EUR": 1.95583,  # EUR/BAM is fixed (currency board)
        "USD": 1.78,
        "GBP": 2.25,
        "CHF": 2.05,
        "CNY": 0.27,
        "TRY": 0.055,
    }
    return fallbacks.get(currency.upper(), 0.0)
