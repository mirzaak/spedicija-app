"""
Dijeljena "sumnjiv tarifni broj" logika — koristi je i xml_generator.py (marker u
XML-u) i pipeline.py (suspect_count za dashboard/notifikaciju). Izdvojeno u zaseban
modul da se izbjegne cirkularni import (pipeline.py već uvozi iz xml_generator.py).
"""

# AI nije "maksimalno siguran" → stavka ide na pregled špediteru.
_SUSPECT_CONFIDENCE = {"low", "medium"}


def is_suspect(item: dict) -> bool:
    """
    True kad AI nije maksimalno siguran u tarifni broj (confidence low/medium).

    Prazan confidence NIJE sumnjiv — to je ručni unos ili ljudski potvrđena
    korekcija, gdje AI nije ni odlučivao.
    """
    return (item.get("confidence") or "").strip().lower() in _SUSPECT_CONFIDENCE


def count_suspects(items: list[dict]) -> tuple[int, list[str]]:
    """Vrati (broj sumnjivih stavki, lista njihovih naziva)."""
    names = [item.get("name", "") for item in items if is_suspect(item)]
    return len(names), names
