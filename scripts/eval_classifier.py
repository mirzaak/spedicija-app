"""
Golden-set eval za AI tarifnu klasifikaciju — PRAVI Claude API pozivi (Haiku + Sonnet), košta.

Mjeri: kad bi se poznata stavka pojavila DANAS bez postojeće korekcije/keyword-mape,
da li bi AI+pravila (claude_classifier._CLASSIFICATION_RULES, _CHAPTER_HINTS, toy override,
invoice-verified logika) sami stigli do istog tarifnog broja koji je čovjek već potvrdio.

Curenje (leakage) — dvije vrste, obje namjerno tolerisane (isto kao backtest_kg_filter.py):
  1. skip_lookups=True zaobilazi correction/keyword_tariff_map/auto_repeat shortcut, ALI
  2. RAG (tariff_vectorstore semantic search) i dalje vidi embedovane opise iz istih tabela,
     pa historijski hint u promptu može posredno "vidjeti" tačan odgovor.
Ovo NIJE čist holdout — mjeri "da li sistem danas i dalje daje tačan odgovor za poznate
slučajeve" (regresija promptova/pravila), ne "generalizuje li AI na potpuno nove stavke".

Ground truth: tariff_corrections (confirmations >= 1) — jedino ljudski potvrđeno.
--include-records dodaje tariff_records (historijske deklaracije, bez eksplicitne potvrde).

Pokretanje (počni sa malim --limit da vidiš trošak/brzinu prije punog runa):
    python scripts/eval_classifier.py --limit 20
    python scripts/eval_classifier.py --sample 200
    python scripts/eval_classifier.py --include-records --limit 50
"""
import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from database import SessionLocal, TariffCorrection, TariffRecord  # noqa: E402
from claude_classifier import classify_items_batch, _clean_code  # noqa: E402

BATCH_SIZE = 10  # stavki po pozivu classify_items_batch — realna veličina pošiljke
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "eval"


def load_pairs(db, include_records: bool) -> list[tuple[str, str, str]]:
    """Vrati [(name, true_code, corpus)]. correction = ljudski potvrđeno; record = historijsko."""
    pairs = []
    for row in db.query(TariffCorrection).filter(TariffCorrection.confirmations >= 1).all():
        code = _clean_code(row.tariff_code)
        name = row.item_name_original or row.item_name_normalized
        if name and code:
            pairs.append((name, code, "correction"))
    if include_records:
        for desc, code in db.query(TariffRecord.description, TariffRecord.tariff_code).all():
            code = _clean_code(code)
            if desc and code:
                pairs.append((desc, code, "record"))
    return pairs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30, help="ograniči broj test parova (default 30 — trošak)")
    ap.add_argument("--sample", type=int, default=0, help="nasumičan uzorak (seed=42) umjesto prvih N")
    ap.add_argument("--include-records", action="store_true",
                     help="dodaj tariff_records (nepotvrđeno, veći ali bučniji korpus)")
    ap.add_argument("--no-save", action="store_true", help="ne snimaj JSON snapshot u data/eval/")
    ap.add_argument("--yes", action="store_true", help="preskoči potvrdu prije pravih API poziva")
    args = ap.parse_args()

    db = SessionLocal()
    pairs = load_pairs(db, args.include_records)

    if args.sample and args.sample < len(pairs):
        import random
        random.Random(42).shuffle(pairs)
        pairs = pairs[: args.sample]
    if args.limit:
        pairs = pairs[: args.limit]

    if not pairs:
        print("Nema ground-truth parova (tariff_corrections prazan?).")
        db.close()
        return

    n_calls_est = (len(pairs) // BATCH_SIZE + 1) * 2  # ~2 Claude poziva po chapter-batchu
    print(f"{len(pairs)} stavki, batch={BATCH_SIZE} → ~{n_calls_est} pravih API poziva (Haiku+Sonnet), NIJE besplatno.")
    if not args.yes:
        resp = input("Nastaviti? [y/N] ").strip().lower()
        if resp != "y":
            print("Prekinuto.")
            db.close()
            return

    results = []
    t0 = time.time()
    for i in range(0, len(pairs), BATCH_SIZE):
        chunk = pairs[i:i + BATCH_SIZE]
        items = [{"name": name} for name, _, _ in chunk]
        classified = classify_items_batch(items, db=db, shipment_context=[], skip_lookups=True)
        for (name, true_code, corpus), got in zip(chunk, classified):
            got_code = _clean_code(got.get("tariff_code", ""))
            results.append({
                "name": name,
                "expected": true_code,
                "got": got_code,
                "match": got_code == true_code,
                "confidence": got.get("confidence", ""),
                "tariff_source": got.get("tariff_source", ""),
                "reason": got.get("reason", ""),
                "corpus": corpus,
            })
        print(f"  ... {min(i + BATCH_SIZE, len(pairs))}/{len(pairs)}", file=sys.stderr)
    elapsed = time.time() - t0
    db.close()

    total = len(results)
    hits = sum(1 for r in results if r["match"])
    accuracy = 100.0 * hits / total if total else 0.0

    by_conf = defaultdict(lambda: {"n": 0, "hits": 0})
    for r in results:
        b = by_conf[r["confidence"] or "?"]
        b["n"] += 1
        b["hits"] += r["match"]

    print(f"\n{'='*70}\nCLASSIFIER EVAL\n{'='*70}")
    print(f"parova:      {total}   ({elapsed:.0f}s)")
    print(f"accuracy:    {accuracy:.1f}%   ({hits}/{total})")
    print(f"\n--- po confidence ---")
    for conf, d in sorted(by_conf.items()):
        pct = 100.0 * d["hits"] / d["n"] if d["n"] else 0.0
        print(f"  {conf:8s}: n={d['n']:4d}  accuracy={pct:5.1f}%")

    misses = [r for r in results if not r["match"]]
    if misses:
        print(f"\n--- promašaji ({len(misses)}) ---")
        for r in misses[:40]:
            print(f"  {r['name'][:45]:45s} expected={r['expected']:10s} got={r['got'] or '(prazno)':10s} "
                  f"conf={r['confidence']:6s} reason={r['reason']}")

    if not args.no_save:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = OUT_DIR / f"classifier_eval_{ts}.json"
        out_path.write_text(json.dumps({
            "timestamp": ts,
            "total": total,
            "accuracy": accuracy,
            "results": results,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nSnimljeno: {out_path}")


if __name__ == "__main__":
    main()
