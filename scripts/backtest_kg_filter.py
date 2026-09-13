"""
Backtest KG filtera za tarifnu klasifikaciju — NULA LLM poziva.

Ovo je dokaz ispravnosti prije nego što se _filter_codes_for_items uključi u produkciju.
Stari VS filter je tiho bacao tačne kodove; jedini način da se to ne ponovi jeste da se
recall izmjeri nad poznatim ground-truth parovima PRIJE uključivanja.

Gate:
  recall@filter = 100.0%  bez holdout-a  (svaki promašaj = bug, ne stvar tuninga)
  recall@filter >= 99.5%  sa holdout-om

Curenje (leakage): graf se gradi iz istih tabela nad kojima testiramo, pa naivni run
daje lažnih 100%. --holdout gradi graf BEZ reda koji se testira:
  - tariff_records:     leave-one-DECLARATION-out po source_file (167 grupa)
  - tariff_corrections: leave-one-out po item_name_normalized
Samo holdout broj nešto znači.

Pokretanje:
    python scripts/backtest_kg_filter.py
    python scripts/backtest_kg_filter.py --holdout
    python scripts/backtest_kg_filter.py --holdout --limit 500
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from database import SessionLocal, OfficialTariff, TariffCorrection, TariffRecord  # noqa: E402
import tariff_graph as tg  # noqa: E402

# Safety floor — mora se poklapati sa _filter_codes_for_items u claude_classifier.py
MIN_FAMILY_CODES = 10    # _MIN_VS_CODES
SMALL_CHAPTER    = 120   # _SMALL_CHAPTER — mala poglavlja se ne filtriraju
MAX_KEEP_RATIO   = 0.6   # filter se ne isplati iznad ovoga


def shadow_filter(item_name, chapter, chapter_codes, db, G,
                  exclude_record_sources=None, exclude_correction_names=None, vs_n=60, strong_only=False):
    """
    Shadow verzija _filter_codes_for_items — ista logika, ali sa injektovanim grafom
    i holdout filterima. Vraća (kept_codes, headings, fallback_reason|None).
    """
    if len(chapter_codes) <= SMALL_CHAPTER:
        return chapter_codes, set(), "small_chapter"

    headings, strong = tg.seed_headings_for(
        [item_name], chapter, db=db, G=G,
        exclude_record_sources=exclude_record_sources,
        exclude_correction_names=exclude_correction_names,
        vs_n=vs_n, with_strength=True,
    )
    # TVRDI DOKAZ je uslov za filtriranje. Meki (VS/graf-token) seed ne generalizuje na
    # neviđene stavke — mjereno: recall 90-98%, tj. tiho bacanje tačnog koda.
    if not strong:
        return chapter_codes, headings, "no_hard_evidence"

    use = strong if strong_only else headings
    family = tg.expand_to_families(use, chapter, G=G)
    headings = use
    if len(family) < MIN_FAMILY_CODES:
        return chapter_codes, headings, "family_too_small"
    if len(family) / len(chapter_codes) > MAX_KEEP_RATIO:
        return chapter_codes, headings, "ratio_too_high"

    kept = [c for c in chapter_codes if c.code in family]
    return kept, headings, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdout", action="store_true",
                    help="leave-one-declaration-out / leave-one-out (jedini pošten broj)")
    ap.add_argument("--limit", type=int, default=0, help="ograniči broj test parova")
    ap.add_argument("--sample", type=int, default=0,
                    help="nasumičan uzorak (seed=42) — za brzo tuniranje")
    ap.add_argument("--vs-n", type=int, default=60, help="koliko VS pogodaka seeduje familije")
    ap.add_argument("--strong-only", action="store_true",
                    help="familije samo iz TVRDOG dokaza (meki VS seed se ignoriše)")
    args = ap.parse_args()

    db = SessionLocal()

    # Kodovi po poglavlju — učitaj jednom
    chapter_codes_cache: dict[str, list] = defaultdict(list)
    for c in db.query(OfficialTariff).order_by(OfficialTariff.code).all():
        chapter_codes_cache[(c.chapter or c.code[:2])[:2]].append(c)

    # --- Korpusi ---
    pairs = []  # (name, true_code, corpus, source_file|None)
    for desc, code, src in db.query(
        TariffRecord.description, TariffRecord.tariff_code, TariffRecord.source_file
    ).all():
        if desc and code:
            pairs.append((desc, code, "record", src))
    for row in db.query(TariffCorrection).filter(TariffCorrection.confirmations >= 1).all():
        if row.tariff_code:
            pairs.append((row.item_name_original or row.item_name_normalized,
                          row.tariff_code, "correction", row.item_name_normalized))

    # Korpus C — dvije regresije koje su ubile stari filter
    regressions = [("DASKA ZA SKLEKOVE", "9506", "95"), ("ŠTAPNI MIKSER", "8509", "85")]

    if args.sample and args.sample < len(pairs):
        import random
        random.Random(42).shuffle(pairs)
        pairs = pairs[: args.sample]
    if args.limit:
        pairs = pairs[: args.limit]

    # --- Grafovi ---
    full_G = tg.build_graph(db)
    holdout_cache: dict[str, "object"] = {}

    def graph_for(corpus, key):
        if not args.holdout:
            return full_G, None, None
        ck = f"{corpus}:{key}"
        if ck not in holdout_cache:
            if corpus == "record":
                holdout_cache[ck] = tg.build_graph(db, exclude_record_sources={key})
            else:
                holdout_cache[ck] = tg.build_graph(db, exclude_correction_names={key})
        G = holdout_cache[ck]
        if corpus == "record":
            return G, {key}, None
        return G, None, {key}

    # --- Run ---
    hits = misses = 0
    unreachable = 0
    fallbacks = defaultdict(int)
    ratios = []
    per_chapter = defaultdict(lambda: {"n": 0, "kept": 0, "total": 0, "miss": 0})
    miss_rows = []

    for i, (name, true_code, corpus, key) in enumerate(pairs):
        ch = tg.chapter_of(true_code)
        if not ch or ch not in chapter_codes_cache:
            continue
        codes = chapter_codes_cache[ch]

        # DOSTIŽNOST: tačan kod mora postojati u official_tariffs PRIJE filtriranja.
        # 28 historijskih zapisa nosi kodove kojih nema u tarifi 2026 (npr. 84219900,
        # 94054010) — stara revizija tarife. Nijedan filter ih ne može zadržati jer
        # nikad nisu bili u skupu kandidata. Da ih brojimo kao promašaje, mjerili bismo
        # kvalitet podataka, ne filtera, i "tunirali" bismo filter koji je već ispravan.
        true8 = "".join(d for d in str(true_code) if d.isdigit())[:8]
        if not any(c.code.startswith(true8) for c in codes):
            unreachable += 1
            continue

        G, ex_src, ex_corr = graph_for(corpus, key)
        kept, headings, fb = shadow_filter(name, ch, codes, db, G, ex_src, ex_corr,
                                           vs_n=args.vs_n, strong_only=args.strong_only)

        # Historijski kodovi su 8-cifreni, official_tariffs.code je 10-cifreni, a 456
        # osmocifrenih prefiksa je dvosmisleno → match po prefiksu prvih 8 cifara.
        kept_set = {c.code for c in kept}
        survived = any(c.startswith(true8) for c in kept_set)

        if fb:
            fallbacks[fb] += 1
        ratios.append(len(kept) / len(codes))
        pc = per_chapter[ch]
        pc["n"] += 1
        pc["kept"] += len(kept)
        pc["total"] += len(codes)

        if survived:
            hits += 1
        else:
            misses += 1
            pc["miss"] += 1
            if len(miss_rows) < 40:
                miss_rows.append((name[:45], true_code, ch, sorted(headings)[:6], fb, len(kept)))

        if args.holdout and (i + 1) % 250 == 0:
            print(f"  ... {i+1}/{len(pairs)}", file=sys.stderr)

    total = hits + misses
    recall = 100.0 * hits / total if total else 0.0
    mean_ratio = sum(ratios) / len(ratios) if ratios else 1.0
    fb_rate = 100.0 * sum(fallbacks.values()) / total if total else 0.0

    mode = "HOLDOUT (leave-one-declaration/one-out)" if args.holdout else "NO-HOLDOUT (curi — očekuj ~100%)"
    print(f"\n{'='*70}\nKG FILTER BACKTEST — {mode}\n{'='*70}")
    print(f"parova:        {total}")
    print(f"nedostižnih:   {unreachable}  (kod ne postoji u tarifi 2026 — kvalitet podataka,"
          f" ne filter; izuzeti iz recall-a)")
    print(f"recall@filter: {recall:.2f}%   ({misses} promašaja)")
    print(f"compression:   {mean_ratio:.1%} prosječno zadržano kodova")
    print(f"fallback_rate: {fb_rate:.1f}%   {dict(fallbacks)}")

    gate = 99.5 if args.holdout else 100.0
    print(f"gate:          >= {gate}%  →  {'PASS ✅' if recall >= gate else 'FAIL ❌'}")

    print(f"\n--- po poglavlju (top 12 po broju parova) ---")
    top = sorted(per_chapter.items(), key=lambda x: -x[1]["n"])[:12]
    for ch, d in top:
        keep_pct = 100.0 * d["kept"] / d["total"] if d["total"] else 100.0
        print(f"  ch{ch}: n={d['n']:4d}  zadržano={keep_pct:5.1f}%  promašaja={d['miss']}")

    if miss_rows:
        print(f"\n--- promašaji (prvih {len(miss_rows)}) ---")
        for name, tc, ch, hds, fb, nk in miss_rows:
            print(f"  ch{ch} {tc:12s} {name:45s} headings={hds} fb={fb} kept={nk}")

    # --- Korpus C: regresije, hard assertion ---
    print(f"\n--- REGRESIJE (kodovi koji su ubili stari filter) ---")
    reg_ok = True
    for name, want_hd, ch in regressions:
        codes = chapter_codes_cache[ch]
        kept, headings, fb = shadow_filter(name, ch, codes, db, full_G)
        kept_set = {c.code for c in kept}
        ok = any(tg.heading_of(c) == want_hd for c in kept_set)
        reg_ok &= ok
        print(f"  {'✅' if ok else '❌'} {name:22s} ch{ch}: familija {want_hd} "
              f"{'preživjela' if ok else 'IZGUBLJENA'}  ({len(kept)}/{len(codes)} kodova, fb={fb})")

    db.close()
    passed = recall >= gate and reg_ok
    print(f"\n{'='*70}\n{'PASS ✅' if passed else 'FAIL ❌'}\n{'='*70}")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
