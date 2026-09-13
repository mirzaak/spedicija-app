"""
Knowledge graph carinske tarife — familijski-zatvoreni retrieval za klasifikaciju.

Zašto graf: stari vectorstore filter je birao POJEDINAČNE kodove po semantičkoj
sličnosti i tiho gubio sibling-e iz iste familije (ŠTAPNI MIKSER → cijela 8509
familija odsječena; DASKA ZA SKLEKOVE → 9506 9190 odsječen). Zbog toga je
_filter_codes_for_items pretvoren u no-op i Sonnetu se šalje cijelo poglavlje.

Graf radi na nivou FAMILIJE (4-cifreni tarifni broj). Rezultat sužavanja je uvijek
unija KOMPLETNIH heading-a, pa se sibling ne može pojedinačno izgubiti — može se
promašiti samo cijeli heading, što pokriva safety floor u pozivaocu.

Hijerarhija se izvodi deterministički iz samog koda (8509400000 → 85 / 8509 /
850940), bez ijednog LLM poziva. NE oslanjaj se na crtice u description_bs —
10.110 od 10.286 redova počinje jednom crticom, dubina tu nije kodirana.

VAŽNO (kanonizacija kodova): official_tariffs.code je 10 cifara, ali historijski
kodovi su 8-cifreni, a 456 osmocifrenih prefiksa je DVOSMISLENO (03031900 →
0303190000 i 0303190010). Zato se naučene ivice sidre na HEADING (4 cifre), koji
je jednoznačan na bilo kojoj dužini koda. Filter ionako troši samo heading.
"""
import logging
import math
import threading
from collections import defaultdict

import networkx as nx
from sqlalchemy import text as _sa_text
from sqlalchemy.orm import Session

from database import SessionLocal, OfficialTariff, TariffCorrection, TariffRecord

logger = logging.getLogger("tariff_graph")

# Težine naučenih ivica po izvoru — korekcije su ljudski potvrđene, records su slabiji signal.
_W_CORRECTION = 3.0
_W_KEYWORD    = 1.0
_W_RECORD     = 1.0

# Koliko VS pogodaka koristimo za seed-ovanje FAMILIJA. Budi velikodušan: expand_to_families
# ionako vraća kompletne heading-e, pa širi seed košta KOMPRESIJU, ne tačnost. Uzak seed
# je ono što obara recall na neviđenim stavkama (mjereno holdout backtest-om).
_VS_SEED_N = 60

_graph: nx.DiGraph | None = None
_lock = threading.Lock()


def heading_of(code: str) -> str | None:
    """4-cifreni tarifni broj (familija) iz koda bilo koje dužine. None ako nema 4 cifre."""
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())
    return digits[:4] if len(digits) >= 4 else None


def chapter_of(code: str) -> str | None:
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())
    return digits[:2] if len(digits) >= 2 else None


def _n_ch(ch: str) -> str:
    return f"ch:{ch}"


def _n_hd(hd: str) -> str:
    return f"hd:{hd}"


def _n_sh(sh: str) -> str:
    return f"sh:{sh}"


def _n_cd(code: str) -> str:
    return f"cd:{code}"


def _n_kw(token: str) -> str:
    return f"kw:{token}"


def _add_learned(G: nx.DiGraph, token: str, code: str, weight: float, source: str) -> bool:
    """kw:TOKEN → hd:XXXX, akumulativna težina. Vraća False ako kod nema heading."""
    hd = heading_of(code)
    if not hd:
        return False
    kn, hn = _n_kw(token), _n_hd(hd)
    if hn not in G:
        return False  # heading ne postoji u zvaničnoj tarifi — ne izmišljaj čvor
    if kn not in G:
        G.add_node(kn, kind="kw", token=token)
    if G.has_edge(kn, hn):
        G[kn][hn]["weight"] += weight
        G[kn][hn]["sources"].add(source)
    else:
        G.add_edge(kn, hn, rel="learned", weight=weight, sources={source})
    return True


def build_graph(
    db: Session,
    exclude_record_sources: set[str] | None = None,
    exclude_correction_names: set[str] | None = None,
) -> nx.DiGraph:
    """
    Izgradi graf iz DB-a. Deterministički, bez LLM-a.

    exclude_* parametri postoje ISKLJUČIVO za holdout u scripts/backtest_kg_filter.py
    (leave-one-declaration-out / leave-one-out). U produkciji se ne koriste — bez njih
    backtest mjeri curenje podataka i daje lažnih 100% recall-a.
    """
    from claude_classifier import _normalize_for_ktm  # lazy: izbjegni cirkularni import

    exclude_record_sources = exclude_record_sources or set()
    exclude_correction_names = exclude_correction_names or set()

    G = nx.DiGraph()
    stats = defaultdict(int)

    # 1. Hijerarhija — jedan prolaz kroz official_tariffs
    for code, chapter in db.query(OfficialTariff.code, OfficialTariff.chapter).all():
        code = (code or "").strip()
        if len(code) < 4:
            stats["skipped_short_code"] += 1
            continue
        ch = (chapter or code[:2]).strip()[:2]
        hd, sh = code[:4], code[:6]
        G.add_node(_n_ch(ch), kind="ch", chapter=ch)
        G.add_node(_n_hd(hd), kind="hd", heading=hd, chapter=ch)
        G.add_node(_n_sh(sh), kind="sh", subheading=sh, chapter=ch)
        G.add_node(_n_cd(code), kind="cd", code=code, chapter=ch, heading=hd)
        G.add_edge(_n_ch(ch), _n_hd(hd), rel="parent", weight=1.0)
        G.add_edge(_n_hd(hd), _n_sh(sh), rel="parent", weight=1.0)
        G.add_edge(_n_sh(sh), _n_cd(code), rel="parent", weight=1.0)
        stats["codes"] += 1

    # 2a. Naučene ivice — tariff_corrections (ljudski potvrđeno, najveća težina)
    for row in db.query(TariffCorrection).filter(TariffCorrection.confirmations >= 1).all():
        if (row.item_name_normalized or "") in exclude_correction_names:
            stats["held_out_correction"] += 1
            continue
        w = _W_CORRECTION * max(1, row.confirmations or 1)
        for tok in _normalize_for_ktm(row.item_name_normalized or row.item_name_original or ""):
            if _add_learned(G, tok, row.tariff_code, w, "correction"):
                stats["e_correction"] += 1
            else:
                stats["unresolved_correction"] += 1

    # 2b. Naučene ivice — keyword_tariff_map (raw SQL, nema ORM modela)
    try:
        rows = db.execute(_sa_text(
            "SELECT keyword, dominant_tariff_code, confidence_score, frequency "
            "FROM keyword_tariff_map"
        )).fetchall()
    except Exception:
        rows = []  # tabela možda ne postoji na staroj instanci
        stats["keyword_table_missing"] = 1
    for kw, code, conf, freq in rows:
        w = _W_KEYWORD * float(conf or 0.0) * math.log1p(float(freq or 0))
        if w <= 0:
            continue
        # keyword može biti n-gram — svaka riječ postaje token, kao u _find_keyword_tariff
        for tok in str(kw or "").upper().split():
            if _add_learned(G, tok, code, w, "keyword"):
                stats["e_keyword"] += 1

    # 2c. Naučene ivice — tariff_records (historijske deklaracije, slab ali brojan signal)
    records = db.query(
        TariffRecord.description, TariffRecord.tariff_code, TariffRecord.source_file
    ).all()
    for desc, code, src in records:
        if src in exclude_record_sources:
            stats["held_out_record"] += 1
            continue
        for tok in _normalize_for_ktm(desc or ""):
            if _add_learned(G, tok, code, _W_RECORD, "record"):
                stats["e_record"] += 1

    # 3. Ko-pojavljivanje heading-a unutar iste deklaracije (source_file grupa).
    #    Samo za prior-e — nikad ne propušta niti odbacuje kodove.
    by_decl: dict[str, set[str]] = defaultdict(set)
    for _desc, code, src in records:
        if src in exclude_record_sources:
            continue  # držana deklaracija ne smije procuriti ni kroz cooc
        hd = heading_of(code)
        if src and hd and _n_hd(hd) in G:
            by_decl[src].add(hd)
    for src, headings in by_decl.items():
        if len(headings) < 2:
            continue
        norm = 1.0 / math.log1p(len(headings))  # velika deklaracija ne smije dominirati
        hl = sorted(headings)
        for i in range(len(hl)):
            for j in range(i + 1, len(hl)):
                a, b = _n_hd(hl[i]), _n_hd(hl[j])
                for u, v in ((a, b), (b, a)):
                    if G.has_edge(u, v) and G[u][v].get("rel") == "cooc":
                        G[u][v]["weight"] += norm
                    elif not G.has_edge(u, v):
                        G.add_edge(u, v, rel="cooc", weight=norm)
                stats["e_cooc"] += 1

    G.graph["stats"] = dict(stats)
    logger.info(
        "Tariff KG: %d čvorova / %d ivica (%s)",
        G.number_of_nodes(), G.number_of_edges(), dict(stats),
    )
    return G


def get_graph() -> nx.DiGraph:
    """Lazy singleton. Gradi na prvi poziv."""
    global _graph
    if _graph is None:
        with _lock:
            if _graph is None:
                db = SessionLocal()
                try:
                    _graph = build_graph(db)
                finally:
                    db.close()
    return _graph


def rebuild(db: Session | None = None) -> dict:
    """Forsiraj rebuild (npr. nakon importa nove tarife)."""
    global _graph
    own = db is None
    db = db or SessionLocal()
    try:
        with _lock:
            _graph = build_graph(db)
        return dict(_graph.graph.get("stats", {}))
    finally:
        if own:
            db.close()


def add_correction(item_name: str, code: str) -> None:
    """
    Inkrementalno dodaj ivice iz nove agentske korekcije — bez punog rebuild-a.
    Zove se iz main.save_correction, uz postojeći tariff_vectorstore.upsert_correction.
    """
    from claude_classifier import _normalize_for_ktm

    if _graph is None:
        return  # graf još nije građen — sljedeći get_graph() će pokupiti korekciju iz DB-a
    for tok in _normalize_for_ktm(item_name or ""):
        _add_learned(_graph, tok, code, _W_CORRECTION, "correction")


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def seed_headings_for(
    item_names: list[str],
    chapter: str,
    db: Session | None = None,
    G: nx.DiGraph | None = None,
    exclude_record_sources: set[str] | None = None,
    exclude_correction_names: set[str] | None = None,
    vs_n: int = _VS_SEED_N,
    with_strength: bool = False,
):
    """
    Skupi 4-cifrene heading-e (familije) iz svih signala za date nazive, ograničeno
    na `chapter`. Izvori: vectorstore, korekcije, keyword_tariff_map, historijski
    zapisi, i kw: ivice grafa.

    with_strength=True → vraća (headings, strong_headings), gdje su STRONG samo one
    familije potvrđene TVRDIM dokazom: agentska korekcija, keyword_tariff_map, ili
    historijski zapis s istim nazivom. VS/graf-token pogoci su MEKI (fuzzy sličnost).

    Zašto ta podjela: holdout backtest pokazuje da meki seed NE generalizuje na neviđene
    stavke (recall 90-98%, i pada što je seed uži) — filtriranje po njemu tiho baca tačan
    kod, tačno ono što je ubilo stari VS filter. Tvrdi dokaz znači "ovu stavku smo već
    vidjeli", i tu je familija pouzdana (recall 100% bez holdout-a).

    G / exclude_* postoje samo za holdout u backtest-u (vidi build_graph). Bez njih bi
    holdout curio kroz korekcije i historijske zapise — baš redove koje testiramo.
    """
    from claude_classifier import _find_keyword_tariff, _normalize_for_ktm, _normalize_name

    exclude_record_sources = exclude_record_sources or set()
    exclude_correction_names = exclude_correction_names or set()
    holdout = bool(exclude_record_sources or exclude_correction_names)

    own = db is None
    db = db or SessionLocal()
    try:
        G = G if G is not None else get_graph()
        headings: set[str] = set()
        strong: set[str] = set()

        def _take(code: str, hard: bool = False) -> None:
            hd = heading_of(code)
            if hd and hd.startswith(chapter) and _n_hd(hd) in G:
                headings.add(hd)
                if hard:
                    strong.add(hd)

        for name in item_names:
            if not name:
                continue

            # 1. Vectorstore — poglavlje već znamo, pa filtriraj po njemu (preciznije
            #    nego chapter=None kako se zove u _classify_batch_and_fill).
            #    VS se gradi iz official_tariffs + korekcija, NE iz tariff_records,
            #    pa za record-holdout nije izvor curenja.
            try:
                import tariff_vectorstore
                for hit in tariff_vectorstore.search(name, chapter=chapter, n=vs_n):
                    _take(hit.get("code", ""))
            except Exception as e:
                logger.debug("VS seed failed for %r: %s", name, e)

            # 2. Agentske korekcije (najjači signal)
            try:
                q = (
                    db.query(TariffCorrection)
                    .filter(TariffCorrection.confirmations >= 1)
                    .filter(TariffCorrection.item_name_normalized == _normalize_name(name))
                )
                for row in q.all():
                    if (row.item_name_normalized or "") in exclude_correction_names:
                        continue
                    _take(row.tariff_code, hard=True)   # TVRDO: ljudski potvrđeno
            except Exception:
                pass

            # 3. keyword_tariff_map
            try:
                kwh = _find_keyword_tariff(name, db)
                if kwh:
                    _take(kwh["tariff_code"], hard=True)  # TVRDO: naučeni keyword, conf >= 0.80
            except Exception:
                pass

            # 4. Historijski zapisi — direktan upit da bi holdout mogao izuzeti
            #    deklaraciju koja se testira (_find_historical to ne dozvoljava).
            try:
                rq = db.query(TariffRecord.tariff_code, TariffRecord.source_file).filter(
                    TariffRecord.description.ilike(f"%{name[:40]}%")
                ).limit(50)
                for code, src in rq.all():
                    if src in exclude_record_sources:
                        continue
                    _take(code, hard=True)   # TVRDO: isti naziv već viđen u ranijoj deklaraciji
            except Exception:
                pass

            # 5. Graf: kw: tokeni → naučeni heading-i (graf je već holdout-ovan)
            try:
                for tok in _normalize_for_ktm(name):
                    kn = _n_kw(tok)
                    if kn not in G:
                        continue
                    for _, hn, d in G.out_edges(kn, data=True):
                        if d.get("rel") == "learned":
                            _take(G.nodes[hn]["heading"])
            except Exception:
                pass

        return (headings, strong) if with_strength else headings
    finally:
        if own:
            db.close()


def expand_to_families(seed_headings: set[str], chapter: str,
                       G: nx.DiGraph | None = None) -> set[str]:
    """
    Familijski-zatvoreno proširenje: vrati SVE listove (10-cifrene kodove) ispod
    svakog pogođenog heading-a u datom poglavlju.

    NO-DROP GARANCIJA: rezultat je unija KOMPLETNIH heading-a. Ako bilo koji seed
    pogodi heading tačnog koda, tačan kod je u rezultatu. Sibling se ne može
    pojedinačno izgubiti.
    """
    G = G if G is not None else get_graph()
    leaves: set[str] = set()
    for hd in seed_headings:
        if not hd.startswith(chapter):
            continue
        hn = _n_hd(hd)
        if hn not in G:
            continue
        for _, sn in G.out_edges(hn):           # hd → sh
            for _, cn in G.out_edges(sn):       # sh → cd
                node = G.nodes[cn]
                if node.get("kind") == "cd":
                    leaves.add(node["code"])
    return leaves


def chapter_priors(item_name: str, db: Session | None = None,
                   context_headings: set[str] | None = None) -> dict[str, float]:
    """
    Skoruj poglavlja za dati naziv preko kw: → hd: ivica, uz cooc bonus za heading-e
    koji istorijski putuju uz ostale stavke iste pošiljke. Normalizovano na 0..1.

    Koristi se SAMO kao hint / za širenje forsiranja — nikad za odbacivanje kodova.
    """
    from claude_classifier import _normalize_for_ktm

    G = get_graph()
    scores: dict[str, float] = defaultdict(float)

    for tok in _normalize_for_ktm(item_name or ""):
        kn = _n_kw(tok)
        if kn not in G:
            continue
        for _, hn, d in G.out_edges(kn, data=True):
            if d.get("rel") != "learned":
                continue
            node = G.nodes[hn]
            w = float(d.get("weight", 0.0))
            if context_headings:
                for chd in context_headings:
                    chn = _n_hd(chd)
                    if G.has_edge(hn, chn) and G[hn][chn].get("rel") == "cooc":
                        w *= 1.0 + min(G[hn][chn]["weight"], 2.0) * 0.25
            scores[node["chapter"]] += w

    total = sum(scores.values())
    if total <= 0:
        return {}
    return {ch: v / total for ch, v in sorted(scores.items(), key=lambda x: -x[1])}
