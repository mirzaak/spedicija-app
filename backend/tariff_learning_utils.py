"""
Zajednička "nahrani vectorstore + knowledge graf" logika — izdvojeno iz
main.py save_correction da je i backend/auto_learn.py može pozvati bez
duplirania (isto ponašanje, jedan izvor istine).
"""
import logging

logger = logging.getLogger("tariff_learning_utils")


def feed_learning_stores(name: str, code: str, desc: str = "") -> None:
    """
    Self-improving petlja: korekcija ide i u vectorstore i u knowledge graf,
    da sljedeća slična stavka odmah seeduje tačnu familiju u Step 2 filteru.
    Non-critical — pad ovdje nikad ne smije oboriti pozivaoca.
    """
    try:
        from tariff_vectorstore import upsert_correction
        upsert_correction(name, code, desc)
    except Exception as e:
        logger.warning(f"vectorstore upsert neuspješan za {name!r}: {e}")
    try:
        from tariff_graph import add_correction
        add_correction(name, code)
    except Exception as e:
        logger.warning(f"tariff_graph add_correction neuspješan za {name!r}: {e}")
