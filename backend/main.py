"""
FastAPI backend for Spedicija automation app.
Run: uvicorn main:app --port 8000
"""
import uuid
import time
import threading
from pathlib import Path
from typing import Optional, Any
from datetime import datetime

from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler

scheduler = BackgroundScheduler()
_system_ready = False  # True when background imports are done

# ── Paths ──────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
UPLOADS_DIR = BASE_DIR / "data" / "uploads"
OUTPUT_DIR = BASE_DIR / "data" / "output"
FRONTEND_DIR = BASE_DIR / "frontend"
DECLARATIONS_DIR = BASE_DIR / "data" / "declarations"

for d in (UPLOADS_DIR, OUTPUT_DIR, DECLARATIONS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ── App ────────────────────────────────────────────────────────────────────
app = FastAPI(title="Spedicija App", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8000", "http://127.0.0.1:8000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


# ── Lazy DB session ────────────────────────────────────────────────────────
def get_db():
    from database import SessionLocal
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Startup ────────────────────────────────────────────────────────────────
@app.on_event("startup")
def startup():
    # Start Gmail scheduler immediately (lazy import on first run)
    creds_exist = (BASE_DIR / "credentials.json").exists()
    if creds_exist:
        def _lazy_pipeline():
            from pipeline import run_pipeline
            run_pipeline()

        scheduler.add_job(_lazy_pipeline, "interval", minutes=5, id="gmail_poll",
                          max_instances=1, misfire_grace_time=60)
        scheduler.start()
        print("[startup] Gmail scheduler pokrenut (svakih 5 min)")

    def _retry_import(name, retries=8, delay=15):
        for attempt in range(retries):
            try:
                import importlib
                return importlib.import_module(name)
            except (TimeoutError, OSError) as e:
                if attempt < retries - 1:
                    print(f"[startup] macOS timeout pri '{name}' (pokušaj {attempt+1}/{retries}), čekam {delay}s...")
                    time.sleep(delay)
                else:
                    raise

    def _background_startup():
        global _system_ready
        print("[startup] Učitavam module...")

        # 1. Baza (SQLAlchemy može timeoutati na macOS — retry)
        for attempt in range(8):
            try:
                from database import init_db, SessionLocal, TariffRecord
                init_db()
                print("[startup] Baza OK")
                break
            except (TimeoutError, OSError) as e:
                if attempt < 7:
                    print(f"[startup] macOS timeout baza (pokušaj {attempt+1}/8), čekam 15s...")
                    time.sleep(15)
                else:
                    print(f"[startup] GREŠKA baza: {e}")
                    return

        # 2. Pipeline i svi teški moduli
        try:
            _retry_import("pipeline")
            _retry_import("pdf_parser")
            _retry_import("claude_classifier")
            _retry_import("drive_uploader")
            print("[startup] Svi moduli učitani — sistem spreman!")
        except Exception as e:
            print(f"[startup] Upozorenje pri učitavanju modula: {e}")

        # 3. Tariff knowledge base
        try:
            from xml_parser import load_all_declarations
            result = load_all_declarations()
            print(f"[startup] Tariff KB: {result['records']} novih zapisa")
        except Exception as e:
            print(f"[startup] Upozorenje xml_parser: {e}")

        try:
            from tariff_pdf_parser import load_official_tariffs
            load_official_tariffs()
        except Exception as e:
            print(f"[startup] Upozorenje tariff_pdf_parser: {e}")

        # 4. Tariff vector store (ChromaDB) — build on first run, load on subsequent
        try:
            from tariff_vectorstore import get_collection
            col = get_collection()
            print(f"[startup] Tariff vectorstore: {col.count()} kodova indeksirano")
        except Exception as e:
            print(f"[startup] Upozorenje vectorstore: {e}")

        # 5. Tariff knowledge graph — familijski filter za Step 2 (deterministički, ~0.5s)
        try:
            from tariff_graph import get_graph
            g = get_graph()
            print(f"[startup] Tariff KG: {g.number_of_nodes()} čvorova / {g.number_of_edges()} ivica")
        except Exception as e:
            print(f"[startup] Upozorenje tariff_graph: {e}")

        _system_ready = True
        print("[startup] ✅ Sistem potpuno spreman!")

    threading.Thread(target=_background_startup, daemon=True).start()
    print("[startup] Server spreman! Moduli se učitavaju u pozadini...")


@app.on_event("shutdown")
def shutdown():
    if scheduler.running:
        scheduler.shutdown(wait=False)


# ── Frontend ───────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def index():
    html_path = FRONTEND_DIR / "dashboard.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    html_path = FRONTEND_DIR / "index.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return HTMLResponse("<h1>Spedicija App</h1><p>Frontend not found.</p>")


@app.get("/legacy", response_class=HTMLResponse)
def legacy_ui():
    html_path = FRONTEND_DIR / "index.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return HTMLResponse("<h1>index.html not found</h1>")


@app.get("/sad-preview", response_class=HTMLResponse)
def sad_preview_mockup():
    """KORAK 1 statički mockup ASYCUDA SAD obrazca (hardkodirani NERMUS podaci)."""
    html_path = FRONTEND_DIR / "sad_mockup.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return HTMLResponse("<h1>Mockup nije nađen</h1>")


@app.get("/sad-editor", response_class=HTMLResponse)
def sad_editor(id: str | None = None):
    """SAD editor — PNG pozadina + HTML overlay inputi. ?id=<shipment_id> za pravo učitavanje."""
    html_path = FRONTEND_DIR / "sad_editor.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return HTMLResponse("<h1>sad_editor.html nije nađen</h1>")


@app.get("/sad", response_class=HTMLResponse)
def sad_view():
    html_path = FRONTEND_DIR / "sad.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return HTMLResponse("<h1>SAD view not found.</h1>")


# ── Pydantic models ────────────────────────────────────────────────────────
class ItemModel(BaseModel):
    name: str
    quantity: float = 1.0
    value: float = 0.0
    gross_weight: float = 0.0
    net_weight: float = 0.0
    tariff_code: str = ""
    country_origin: str = "CN"
    official_desc: str = ""
    currency: str = "EUR"
    # Bez ovih polja Pydantic tiho odbaci AI confidence na ručnom /generate putu,
    # pa xml_generator ne može označiti sumnjiv tarifni broj (_is_suspect).
    confidence: str = ""
    tariff_source: str = ""


class GenerateRequest(BaseModel):
    items: list[ItemModel]
    consignee: str = ""
    consignee_code: str = ""
    consignee_address: str = ""
    invoice_number: str = ""
    exporter_name: str = ""
    exporter_city: str = ""
    exporter_street: str = ""
    exporter_country: str = "CN"
    currency: str = "EUR"
    currency_rate: float = 0.0
    container_number: str = ""
    transport_identity: str = ""
    transport_nationality: str = "BA"
    border_office_code: str = ""
    border_office_name: str = ""
    transit_doc: str = ""
    eur1_reference: str = ""
    delivery_place: str = ""
    delivery_terms: str = "CIF"
    external_freight: float = 0.0
    external_freight_currency: str = "EUR"
    internal_freight: float = 0.0
    insurance: float = 0.0
    total_packages: int = 0
    declaration_date: str = ""


class TariffSuggestion(BaseModel):
    id: int
    description: str
    tariff_code: str
    official_desc: str
    country_origin: str
    unit_value: Optional[float]
    source_file: str


# ── Pipeline manual file processing ──────────────────────────────────────
@app.post("/pipeline/process-files")
async def process_files_manual(files: list[UploadFile] = File(...)):
    """Full pipeline for manually uploaded files — same as Gmail pipeline."""
    import re as _re
    from datetime import datetime
    from pipeline import _analyze_shipment_with_claude, _auto_assign_tariffs
    from excel_generator import generate_excel
    from xml_generator import generate_asycuda_xml
    from database import SessionLocal, get_setting

    saved_files = []
    for f in files:
        suffix = Path(f.filename or "").suffix.lower()
        ct = (f.content_type or "").lower()
        if suffix not in (".pdf", ".xlsx", ".xls"):
            if "pdf" in ct:
                suffix = ".pdf"
            elif "spreadsheet" in ct or "excel" in ct:
                suffix = ".xlsx"
            else:
                continue
        save_path = UPLOADS_DIR / f"{uuid.uuid4()}{suffix}"
        content = await f.read()
        save_path.write_bytes(content)
        saved_files.append({"filename": f.filename or f"file{suffix}", "filepath": str(save_path)})

    if not saved_files:
        raise HTTPException(400, "Nema validnih fajlova (PDF ili Excel)")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    subject = saved_files[0]["filename"]

    try:
        shipment_data = _analyze_shipment_with_claude(saved_files, subject)
    except Exception as e:
        raise HTTPException(500, f"Claude analiza greška: {e}")

    items = shipment_data["items"]
    if not items:
        raise HTTPException(422, "Nema stavki robe pronađeno u dokumentima")

    db = SessionLocal()
    try:
        items = _auto_assign_tariffs(items, db)
        declarant_code = get_setting(db, "declarant_code")
        declarant_name = get_setting(db, "declarant_name")
        declarant_ref  = get_setting(db, "declarant_ref")
        office_code    = get_setting(db, "office_code", "BA010301")
        office_name    = get_setting(db, "office_name", "CI Tuzla")
    finally:
        db.close()

    consignee = shipment_data.get("consignee", "")
    inv_num   = _re.sub(r"[^\w-]", "", shipment_data.get("invoice_number", "")).strip()
    excel_name = f"{ts}_manual.xlsx"
    xml_name   = f"{inv_num}.xml" if inv_num else f"{ts}_manual.xml"
    excel_path = str(OUTPUT_DIR / excel_name)
    xml_path   = str(OUTPUT_DIR / xml_name)

    try:
        generate_excel(items, excel_path, consignee=consignee)
    except Exception as e:
        raise HTTPException(500, f"Excel greška: {e}")

    try:
        generate_asycuda_xml(
            items=items,
            consignee_name=consignee,
            consignee_code=shipment_data.get("consignee_pib", ""),
            consignee_address=shipment_data.get("consignee_address", ""),
            exporter_name=shipment_data.get("exporter_name", ""),
            exporter_city=shipment_data.get("exporter_city", ""),
            exporter_street=shipment_data.get("exporter_street", ""),
            exporter_country=shipment_data.get("exporter_country", "CN"),
            currency=shipment_data.get("currency", "EUR"),
            currency_rate=shipment_data.get("currency_rate", 0.0),
            declaration_date=ts[:8],
            container_number=shipment_data.get("container_number", ""),
            transport_identity=shipment_data.get("transport_identity", ""),
            transport_nationality=shipment_data.get("transport_nationality", "BA"),
            border_office_code=shipment_data.get("border_office_code", ""),
            border_office_name=shipment_data.get("border_office_name", ""),
            transit_doc=shipment_data.get("transit_doc", ""),
            delivery_terms=shipment_data.get("delivery_terms", "CIF"),
            delivery_place=shipment_data.get("delivery_place", ""),
            invoice_number=shipment_data.get("invoice_number", ""),
            eur1_reference=shipment_data.get("eur1_reference", ""),
            external_freight=shipment_data.get("external_freight", 0.0),
            external_freight_currency=shipment_data.get("external_freight_currency", "EUR"),
            internal_freight=shipment_data.get("internal_freight", 0.0),
            insurance=shipment_data.get("insurance", 0.0),
            packages_count=shipment_data.get("total_packages", 0),
            declarant_code=declarant_code,
            declarant_name=declarant_name,
            declarant_ref=declarant_ref,
            office_code=office_code or "BA010301",
            office_name=office_name or "CI Tuzla",
            output_path=xml_path,
        )
    except Exception as e:
        raise HTTPException(500, f"XML greška: {e}")

    if Path(xml_path).exists():
        try:
            from xml_parser import parse_xml_file
            db2 = SessionLocal()
            try:
                parse_xml_file(xml_path, db2)
            finally:
                db2.close()
        except Exception:
            pass

    drive_links = {}
    try:
        from drive_uploader import upload_declaration_files
        from pipeline import _safe_folder_name
        folder_name = _safe_folder_name(subject, ts)
        drive_links = upload_declaration_files(
            subfolder_name=folder_name,
            original_path=saved_files[0]["filepath"],
            excel_path=excel_path,
            xml_path=xml_path,
        )
    except Exception as e:
        drive_links = {"error": str(e)}

    return {
        "items": items,
        "metadata": {k: v for k, v in shipment_data.items() if k != "items"},
        "excel": {"filename": excel_name, "download_url": f"/download/{excel_name}"},
        "xml":   {"filename": xml_name,   "download_url": f"/download/{xml_name}"},
        "drive": drive_links,
    }


# ── Upload & Parse ─────────────────────────────────────────────────────────
@app.post("/upload")
async def upload_invoice(file: UploadFile = File(...)):
    suffix = Path(file.filename or "").suffix.lower()
    ct = (file.content_type or "").lower()
    if suffix not in (".pdf", ".xlsx", ".xls"):
        if "pdf" in ct:
            suffix = ".pdf"
        elif "spreadsheet" in ct or "excel" in ct:
            suffix = ".xlsx"
        else:
            raise HTTPException(400, f"Nepodržan format '{suffix}' ({ct}). Prihvatamo: PDF, XLSX, XLS.")

    save_path = UPLOADS_DIR / f"{uuid.uuid4()}{suffix}"
    content = await file.read()
    save_path.write_bytes(content)

    try:
        from pdf_parser import parse_pdf, parse_excel_invoice
        if suffix == ".pdf":
            items, currency = parse_pdf(str(save_path))
        else:
            items, currency = parse_excel_invoice(str(save_path))
    except Exception as e:
        raise HTTPException(500, f"Failed to parse file: {e}")

    return {
        "filename": file.filename,
        "currency": currency,
        "items": [
            {
                "name": it.name,
                "quantity": it.quantity,
                "value": round(it.total_value, 2),
                "gross_weight": round(it.gross_weight, 3),
                "net_weight": round(it.net_weight, 3),
                "tariff_code": it.tariff_code,
                "country_origin": it.country_origin,
                "currency": it.currency,
            }
            for it in items
        ],
    }


# ── Tariff Search ──────────────────────────────────────────────────────────
@app.get("/tariffs/suggest", response_model=list[TariffSuggestion])
def suggest_tariffs(
    q: str = Query(..., min_length=2),
    limit: int = Query(10, le=50),
    db: Any = Depends(get_db),
):
    from xml_parser import search_tariff
    results = search_tariff(q, db, limit=limit)
    return [
        TariffSuggestion(
            id=r.id,
            description=r.description,
            tariff_code=r.tariff_code,
            official_desc=r.official_desc or "",
            country_origin=r.country_origin or "",
            unit_value=r.unit_value,
            source_file=r.source_file or "",
        )
        for r in results
    ]


@app.get("/tariffs/all")
def all_tariffs(db: Any = Depends(get_db)):
    from database import TariffRecord
    records = db.query(TariffRecord).order_by(TariffRecord.tariff_code).all()
    seen = set()
    result = []
    for r in records:
        if r.tariff_code not in seen:
            seen.add(r.tariff_code)
            result.append({
                "tariff_code": r.tariff_code,
                "official_desc": r.official_desc,
                "example_description": r.description,
            })
    return result


# ── Generate Output ────────────────────────────────────────────────────────
@app.post("/generate/excel")
def generate_excel_endpoint(req: GenerateRequest):
    from excel_generator import generate_excel
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"deklaracija_{ts}.xlsx"
    items_dicts = [item.dict() for item in req.items]
    generate_excel(items_dicts, str(OUTPUT_DIR / filename), consignee=req.consignee)
    return {"filename": filename, "download_url": f"/download/{filename}"}


def _xml_kwargs(req: GenerateRequest, output_path: str, db=None) -> dict:
    from database import get_setting, SessionLocal
    if db is None:
        db = SessionLocal()
        close_db = True
    else:
        close_db = False
    try:
        declarant_code = get_setting(db, "declarant_code")
        declarant_name = get_setting(db, "declarant_name")
        declarant_ref  = get_setting(db, "declarant_ref")
        office_code    = get_setting(db, "office_code", "BA010301")
        office_name    = get_setting(db, "office_name", "CI Tuzla")
    finally:
        if close_db:
            db.close()
    return dict(
        items=[item.dict() for item in req.items],
        consignee_name=req.consignee,
        consignee_code=req.consignee_code,
        consignee_address=req.consignee_address,
        exporter_name=req.exporter_name,
        exporter_city=req.exporter_city,
        exporter_street=req.exporter_street,
        exporter_country=req.exporter_country,
        currency=req.currency,
        currency_rate=req.currency_rate,
        declaration_date=req.declaration_date,
        container_number=req.container_number,
        transport_identity=req.transport_identity,
        transport_nationality=req.transport_nationality or "BA",
        border_office_code=req.border_office_code,
        border_office_name=req.border_office_name,
        transit_doc=req.transit_doc,
        delivery_terms=req.delivery_terms,
        delivery_place=req.delivery_place,
        invoice_number=req.invoice_number,
        eur1_reference=req.eur1_reference,
        external_freight=req.external_freight,
        external_freight_currency=req.external_freight_currency or "EUR",
        internal_freight=req.internal_freight,
        insurance=req.insurance,
        packages_count=req.total_packages,
        declarant_code=declarant_code,
        declarant_name=declarant_name,
        declarant_ref=declarant_ref,
        office_code=office_code or "BA010301",
        office_name=office_name or "CI Tuzla",
        output_path=output_path,
        db=db,
    )


@app.post("/generate/xml")
def generate_xml_endpoint(req: GenerateRequest, db: Any = Depends(get_db)):
    from xml_generator import generate_asycuda_xml
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"deklaracija_{ts}.xml"
    generate_asycuda_xml(**_xml_kwargs(req, str(OUTPUT_DIR / filename), db))
    return {"filename": filename, "download_url": f"/download/{filename}"}


@app.post("/generate/both")
def generate_both(req: GenerateRequest, db: Any = Depends(get_db)):
    from excel_generator import generate_excel
    from xml_generator import generate_asycuda_xml
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    excel_name = f"deklaracija_{ts}.xlsx"
    xml_name   = f"deklaracija_{ts}.xml"
    generate_excel([item.dict() for item in req.items], str(OUTPUT_DIR / excel_name), consignee=req.consignee)
    generate_asycuda_xml(**_xml_kwargs(req, str(OUTPUT_DIR / xml_name), db))
    return {
        "excel": {"filename": excel_name, "download_url": f"/download/{excel_name}"},
        "xml":   {"filename": xml_name,   "download_url": f"/download/{xml_name}"},
    }


# ── Download ───────────────────────────────────────────────────────────────
@app.get("/download/{filename}")
def download_file(filename: str):
    filepath = OUTPUT_DIR / filename
    if not filepath.exists():
        raise HTTPException(404, "File not found.")
    media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" \
        if filename.endswith(".xlsx") else "application/xml"
    return FileResponse(str(filepath), filename=filename, media_type=media_type)


# ── Official tariff PDF upload ────────────────────────────────────────────
@app.post("/tariffs/upload-official-pdf")
async def upload_official_tariff_pdf(file: UploadFile = File(...)):
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Samo PDF fajlovi su prihvaćeni.")
    from tariff_pdf_parser import TARIFF_PDF_PATH, load_official_tariffs
    from database import OfficialTariff, SessionLocal

    content = await file.read()
    TARIFF_PDF_PATH.write_bytes(content)

    t = threading.Thread(target=lambda: load_official_tariffs(force_reload=True))
    t.start()
    t.join(timeout=120)

    db2 = SessionLocal()
    total = db2.query(OfficialTariff).count()
    db2.close()
    return {"message": f"Tarifa učitana. Ukupno tarifnih stavki u bazi: {total}", "records": total}


@app.get("/tariffs/official-browse")
def browse_official_tariffs(
    q: str = Query("", description="Search term"),
    chapter: str = Query("", description="Filter by chapter"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, le=500),
    db: Any = Depends(get_db),
):
    from database import OfficialTariff
    query = db.query(OfficialTariff)
    if chapter:
        query = query.filter(OfficialTariff.chapter == chapter.zfill(2))
    if q:
        from sqlalchemy import or_, func
        q_lower = f"%{q.lower()}%"
        query = query.filter(
            or_(
                func.lower(OfficialTariff.description_bs).like(q_lower),
                OfficialTariff.code.like(f"{q}%"),
            )
        )
    total = query.count()
    records = query.order_by(OfficialTariff.code).offset((page - 1) * page_size).limit(page_size).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "results": [{"code": r.code, "description": r.description_bs, "chapter": r.chapter} for r in records],
    }


@app.get("/tariffs/chapters")
def list_chapters(db: Any = Depends(get_db)):
    from database import OfficialTariff
    from sqlalchemy import func
    rows = db.query(OfficialTariff.chapter, func.count(OfficialTariff.id), func.min(OfficialTariff.description_bs)) \
        .group_by(OfficialTariff.chapter).order_by(OfficialTariff.chapter).all()
    return [{"chapter": r[0], "count": r[1], "first_desc": r[2]} for r in rows]


@app.get("/tariffs/browser", response_class=HTMLResponse)
def tariff_browser():
    html_path = FRONTEND_DIR / "tariff_browser.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return HTMLResponse("<h1>Tariff browser not found.</h1>")


@app.get("/tariffs/official-stats")
def official_tariff_stats(db: Any = Depends(get_db)):
    from database import OfficialTariff
    from tariff_pdf_parser import TARIFF_PDF_PATH
    total = db.query(OfficialTariff).count()
    chapters = db.query(OfficialTariff.chapter).distinct().count()
    return {
        "total_codes": total,
        "chapters": chapters,
        "pdf_cached": TARIFF_PDF_PATH.exists(),
        "pdf_size_mb": round(TARIFF_PDF_PATH.stat().st_size / 1_000_000, 1) if TARIFF_PDF_PATH.exists() else 0,
    }


# ── XML Upload to Knowledge Base ──────────────────────────────────────────
@app.post("/declarations/upload-xml")
async def upload_xml_declaration(file: UploadFile = File(...), db: Any = Depends(get_db)):
    if not file.filename.endswith(".xml"):
        raise HTTPException(400, "Only XML files accepted.")
    from xml_parser import parse_xml_file
    save_path = DECLARATIONS_DIR / file.filename
    content = await file.read()
    save_path.write_bytes(content)
    count = parse_xml_file(str(save_path), db)
    return {"message": f"Added {count} new tariff records from {file.filename}"}


@app.get("/declarations")
def list_declarations(db: Any = Depends(get_db)):
    from database import Declaration
    decls = db.query(Declaration).order_by(Declaration.created_at.desc()).all()
    return [
        {
            "id": d.id,
            "filename": d.filename,
            "consignee": d.consignee,
            "date": d.declaration_date,
            "currency": d.currency,
            "items": d.total_items,
        }
        for d in decls
    ]


# ── Pipeline status & manual trigger ──────────────────────────────────────
@app.get("/pipeline/pending")
def get_pending_shipments(db: Any = Depends(get_db)):
    import json
    from database import PendingShipment
    rows = db.query(PendingShipment).filter_by(status="pending").order_by(PendingShipment.created_at.desc()).all()
    return [
        {
            "id": r.id,
            "subject": r.email_subject,
            "sender": r.sender,
            "received_at": r.received_at,
            "files": json.loads(r.files_json or "[]"),
            "status": r.status,
        }
        for r in rows
    ]


@app.get("/pipeline/extracted")
def get_extracted_shipments(db: Any = Depends(get_db)):
    """
    Semi-auto tok: lista pošiljki kojima je extract+klasifikacija gotova,
    Čekaju agentov pregled prije finalize/Drive uploada.
    """
    import json
    from database import PendingShipment
    rows = (
        db.query(PendingShipment)
        .filter_by(status="extracted")
        .order_by(PendingShipment.extracted_at.desc())
        .all()
    )
    result = []
    for r in rows:
        items = json.loads(r.extracted_items_json or "[]")
        metadata = json.loads(r.extracted_metadata_json or "{}")
        empty_codes = sum(1 for i in items if not (i.get("tariff_code") or "").strip())
        result.append({
            "id": r.id,
            "subject": r.email_subject,
            "sender": r.sender,
            "received_at": r.received_at,
            "extracted_at": r.extracted_at.isoformat() if r.extracted_at else None,
            "items_count": len(items),
            "empty_codes": empty_codes,
            "invoice_number": metadata.get("invoice_number", ""),
            "consignee": metadata.get("consignee", ""),
            "exporter_name": metadata.get("exporter_name", ""),
        })
    return result


@app.get("/pipeline/uploaded")
def get_uploaded_shipments(db: Any = Depends(get_db)):
    """
    Sve završene pošiljke — i semi-auto ("uploaded") i full-auto ("approved",
    89% obima danas). Ranije se full-auto tok nigdje u dashboardu nije prikazivao,
    pa je bio nevidljiv iako je Drive upload već bio gotov.
    """
    import json
    from database import PendingShipment
    from classification_utils import is_suspect
    rows = (
        db.query(PendingShipment)
        .filter(PendingShipment.status.in_(["uploaded", "approved"]))
        .order_by(PendingShipment.finalized_at.desc())
        .all()
    )
    result = []
    for r in rows:
        items = json.loads(r.extracted_items_json or "[]")
        suspect_count = sum(1 for i in items if is_suspect(i))
        result.append({
            "id": r.id,
            "email_subject": r.email_subject,
            "drive_folder": r.drive_folder,
            "drive_url": r.drive_url,
            "finalized_at": r.finalized_at.isoformat() if r.finalized_at else None,
            "item_count": len(items),
            "suspect_count": suspect_count,
            "source": "semi-auto" if r.status == "uploaded" else "full-auto",
        })
    return result


@app.get("/pipeline/load-extracted/{shipment_id}")
def load_extracted_shipment(shipment_id: int, db: Any = Depends(get_db)):
    """
    Vraća parsed items+metadata za UI editor. Koristi se kad agent klikne
    na 'Za pregled' stavku da je uČita u 'Nova deklaracija' tab.
    """
    import json
    from database import PendingShipment
    row = (
        db.query(PendingShipment)
        .filter(PendingShipment.id == shipment_id)
        .filter(PendingShipment.status.in_(["extracted", "uploaded", "approved"]))
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Shipment not found or not in 'extracted'/'uploaded'/'approved' status")
    return {
        "id": row.id,
        "subject": row.email_subject,
        "items": json.loads(row.extracted_items_json or "[]"),
        "metadata": json.loads(row.extracted_metadata_json or "{}"),
        "extracted_at": row.extracted_at.isoformat() if row.extracted_at else None,
    }


@app.post("/pipeline/approve/{shipment_id}")
def approve_shipment(shipment_id: int, db: Any = Depends(get_db)):
    import json
    from database import PendingShipment
    from pipeline import process_shipment, _log
    from gmail_watcher import mark_gmail_processed

    row = db.query(PendingShipment).filter_by(id=shipment_id, status="pending").first()
    if not row:
        raise HTTPException(status_code=404, detail="Shipment not found or already processed")

    # Do NOT commit status before the thread — follows the same pattern as run_pipeline().
    # Status is written in one transaction AFTER successful process_shipment(); if it
    # throws, the row stays "pending" and will be retried on the next scheduler run.
    shipment = {
        "email_subject": row.email_subject,
        "received_at": row.received_at,
        "files": json.loads(row.files_json or "[]"),
    }
    gmail_id = row.gmail_message_id
    subject = row.email_subject
    shipment_id_local = shipment_id

    def _run():
        from database import SessionLocal as _Sess, PendingShipment as _PS
        try:
            result = process_shipment(shipment)
            if result["status"] == "ok":
                _db = _Sess()
                try:
                    _row = _db.query(_PS).filter_by(id=shipment_id_local).first()
                    if _row:
                        drive_links = result.get("drive_links") or {}
                        _row.extracted_items_json = json.dumps(result.get("items", []), ensure_ascii=False)
                        _row.drive_folder = result.get("drive_folder", "")
                        _row.drive_url = drive_links.get("folder_url") or drive_links.get("xml") or ""
                        _row.extracted_at = datetime.utcnow()
                        _row.finalized_at = datetime.utcnow()
                        _row.status = "approved"
                        _db.commit()
                finally:
                    _db.close()
                _log(f"✅ Obrađeno: '{subject}'")
                try:
                    from googleapiclient.discovery import build
                    from google_auth import get_credentials
                    service = build("gmail", "v1", credentials=get_credentials(), cache_discovery=False)
                    mark_gmail_processed(service, gmail_id)
                except Exception:
                    pass
            else:
                _log(f"⚠ '{subject}': {result.get('reason') or result.get('error')}", "warning")
        except Exception as e:
            _log(f"Greška: '{subject}': {e}", "error")

    threading.Thread(target=_run, daemon=True).start()
    return {"message": f"Obrada pokrenuta za: {row.email_subject}"}


# ─── SEMI-AUTO TOK — paralelno uz legacy /approve ──────────────────────────
# /approve = legacy "sve odjednom" tok (i dalje radi, 51 historijska deklaracija)
# /prepare = NOVO — samo ekstrakcija + klasifikacija, Drive NIJE dotaknut
# /finalize = NOVO — Excel+XML+Drive upload, zove se isključivo nakon agentove
#             potvrde s edited items+metadata iz UI-a.


class FinalizeRequest(BaseModel):
    """Body za /pipeline/finalize/{id}. Items i metadata su agentom uređeni."""
    items: list[dict]
    metadata: dict


@app.post("/pipeline/prepare/{shipment_id}")
def prepare_shipment(shipment_id: int, db: Any = Depends(get_db)):
    """
    SEMI-AUTO TOK korak 1: pokreni ekstrakciju + klasifikaciju za pending mail.

    Pozadinska obrada (fire-and-forget). Status: pending → extracted.
    Drive NIJE dotaknut iz ovog endpoint-a. Klijent može poll-ati
    /pipeline/pending za status update (kad postane 'extracted', dostupni
    su extracted_items_json + extracted_metadata_json za UI pregled).
    """
    import json
    from database import PendingShipment
    from pipeline import process_shipment_extract, _log

    row = db.query(PendingShipment).filter_by(id=shipment_id, status="pending").first()
    if not row:
        raise HTTPException(
            status_code=404,
            detail="Shipment not found or not in 'pending' status"
        )

    shipment = {
        "email_subject": row.email_subject,
        "received_at": row.received_at,
        "files": json.loads(row.files_json or "[]"),
    }
    subject = row.email_subject

    def _run():
        from database import SessionLocal as _Sess
        try:
            result = process_shipment_extract(shipment)
            _db = _Sess()
            try:
                _row = _db.query(PendingShipment).filter_by(id=shipment_id).first()
                if not _row:
                    return
                if result["status"] == "ok":
                    _row.extracted_items_json = json.dumps(result["items"], ensure_ascii=False)
                    _row.extracted_metadata_json = json.dumps(result["metadata"], ensure_ascii=False)
                    _row.extracted_at = datetime.utcnow()
                    _row.status = "extracted"
                    _row.extract_error = None
                    _log(f"✅ Pripremljeno za pregled: '{subject}' ({result['items_count']} stavki)")
                else:
                    _row.extract_error = result.get("error") or result.get("reason") or "unknown"
                    _log(f"⚠ Prepare neuspješan: '{subject}': {_row.extract_error}", "warning")
                _db.commit()
            finally:
                _db.close()
        except Exception as e:
            _log(f"Prepare greška: '{subject}': {e}", "error")
            _db = _Sess()
            try:
                _row = _db.query(PendingShipment).filter_by(id=shipment_id).first()
                if _row:
                    _row.extract_error = str(e)
                    _db.commit()
            finally:
                _db.close()

    threading.Thread(target=_run, daemon=True).start()
    return {"message": f"Priprema pokrenuta: {row.email_subject}", "shipment_id": shipment_id}


@app.post("/pipeline/finalize/{shipment_id}")
def finalize_shipment(shipment_id: int, req: FinalizeRequest, db: Any = Depends(get_db)):
    """
    SEMI-AUTO TOK korak 2: nakon agentove potvrde generiše XML+Excel i šalje na Drive.

    Status: extracted → uploaded. Koristi `req.items` i `req.metadata` iz body-ja
    (agent je možda editovao stavke u UI-u). Ne koristi originalne extracted_*_json
    iz baze osim kao snapshot za audit.
    """
    import json
    from database import PendingShipment
    from pipeline import process_shipment_finalize, _log
    from gmail_watcher import mark_gmail_processed

    row = db.query(PendingShipment).filter_by(id=shipment_id, status="extracted").first()
    if not row:
        raise HTTPException(
            status_code=404,
            detail="Shipment not found or not in 'extracted' status (run /prepare first)"
        )

    # Warning za prazne tariff_code — ne blokira, samo vraća upozorenje u odgovoru
    empty_codes = [
        i.get("name") or i.get("description") or f"stavka {idx+1}"
        for idx, i in enumerate(req.items)
        if not (i.get("tariff_code") or "").strip()
    ]

    shipment = {
        "email_subject": row.email_subject,
        "received_at": row.received_at,
        "files": json.loads(row.files_json or "[]"),
    }
    gmail_id = row.gmail_message_id
    subject = row.email_subject
    items = req.items
    metadata = req.metadata

    def _run():
        from database import SessionLocal as _Sess
        try:
            result = process_shipment_finalize(shipment, items, metadata)
            _db = _Sess()
            try:
                _row = _db.query(PendingShipment).filter_by(id=shipment_id).first()
                if not _row:
                    return
                if result["status"] == "ok":
                    # Snapshot XML za audit
                    xml_path = result.get("xml_path")
                    if xml_path and Path(xml_path).exists():
                        try:
                            _row.declaration_xml = Path(xml_path).read_text(encoding="utf-8")
                        except Exception:
                            pass
                    _row.drive_folder = result.get("drive_folder", "")
                    drive_links = result.get("drive_links") or {}
                    if isinstance(drive_links, dict):
                        _row.drive_url = drive_links.get("folder_url") or drive_links.get("xml") or ""
                    _row.finalized_at = datetime.utcnow()
                    _row.status = "uploaded"
                    _log(f"✅ Finalizirano + Drive: '{subject}'")
                _db.commit()
                if result["status"] == "ok":
                    # C1-C2: auto-promocija ponavljanja + KTM rebuild, throttled
                    # preko AppSettings — jeftin no-op ako je nedavno već pokrenuto.
                    try:
                        from auto_learn import run_auto_learn_if_due
                        run_auto_learn_if_due()
                    except Exception as e:
                        _log(f"Auto-learn greška (nekritično): {e}", "warning")
            finally:
                _db.close()
            # Označi Gmail "processed" SAMO nakon uspješnog upload-a
            if result["status"] == "ok":
                try:
                    from googleapiclient.discovery import build
                    from google_auth import get_credentials
                    service = build("gmail", "v1", credentials=get_credentials(), cache_discovery=False)
                    mark_gmail_processed(service, gmail_id)
                except Exception:
                    pass
        except Exception as e:
            _log(f"Finalize greška: '{subject}': {e}", "error")

    threading.Thread(target=_run, daemon=True).start()
    resp: dict = {"message": f"Finalizacija pokrenuta: {row.email_subject}", "shipment_id": shipment_id}
    if empty_codes:
        resp["warning"] = f"Prazni tarifni kodovi: {', '.join(empty_codes)}"
    return resp


class SadUpdateRequest(BaseModel):
    shipment_id: int
    f31_description: str | None = None
    f33_tariff_code: str | None = None
    f35_gross_mass: str | None = None
    f38_net_mass: str | None = None


@app.post("/pipeline/sad-update")
def sad_update(req: SadUpdateRequest, db: Any = Depends(get_db)):
    """
    Sprema izmjene iz SAD editora u extracted_items_json.
    Radi SAMO na statusu 'extracted'. Označava stavku sa _edited=True.
    """
    import json
    from database import PendingShipment

    row = db.query(PendingShipment).filter_by(id=req.shipment_id, status="extracted").first()
    if not row:
        raise HTTPException(
            status_code=404,
            detail="Shipment not found or not in 'extracted' status"
        )

    items = json.loads(row.extracted_items_json or "[]")
    if not items:
        raise HTTPException(status_code=422, detail="Nema stavki u extracted_items_json")

    # Ažuriraj prvu stavku (SAD editor prikazuje item 0)
    item = items[0]
    updated_fields = []

    if req.f31_description is not None:
        item["description"] = req.f31_description
        updated_fields.append("description")
    if req.f33_tariff_code is not None:
        item["tariff_code"] = req.f33_tariff_code.strip()
        item["_edited"] = True   # signal za KTM učenje
        updated_fields.append("tariff_code")
    if req.f35_gross_mass is not None:
        item["gross_weight"] = req.f35_gross_mass
        updated_fields.append("gross_weight")
    if req.f38_net_mass is not None:
        item["net_weight"] = req.f38_net_mass
        updated_fields.append("net_weight")

    items[0] = item
    row.extracted_items_json = json.dumps(items, ensure_ascii=False)
    db.commit()

    return {"success": True, "updated_fields": updated_fields, "shipment_id": req.shipment_id}


@app.post("/pipeline/skip/{shipment_id}")
def skip_shipment(shipment_id: int, db: Any = Depends(get_db)):
    from database import PendingShipment
    from gmail_watcher import mark_gmail_processed

    row = db.query(PendingShipment).filter_by(id=shipment_id, status="pending").first()
    if not row:
        raise HTTPException(status_code=404, detail="Shipment not found or already processed")

    row.status = "skipped"
    db.commit()

    try:
        from googleapiclient.discovery import build
        from google_auth import get_credentials
        service = build("gmail", "v1", credentials=get_credentials(), cache_discovery=False)
        mark_gmail_processed(service, row.gmail_message_id)
    except Exception:
        pass

    return {"message": f"Preskočeno: {row.email_subject}"}


@app.get("/pipeline/log")
def pipeline_log_endpoint():
    from pipeline import pipeline_log
    return list(reversed(pipeline_log))


@app.post("/pipeline/log/clear")
def pipeline_log_clear():
    from pipeline import pipeline_log
    pipeline_log.clear()
    return {"ok": True}


@app.post("/pipeline/run-now")
def pipeline_run_now():
    from pipeline import run_pipeline
    threading.Thread(target=run_pipeline, daemon=True).start()
    return {"message": "Pipeline pokrenut u pozadini. Provjeri /pipeline/log za status."}


@app.post("/gmail/reset-ignored")
def gmail_reset_ignored():
    """Remove Spedicija-Ignored label from recent emails so they get re-processed."""
    try:
        from googleapiclient.discovery import build
        from google_auth import get_credentials
        from gmail_watcher import IGNORED_LABEL, PENDING_LABEL, PROCESSED_LABEL, _get_or_create_label
        creds = get_credentials()
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        ignored_id = _get_or_create_label(service, IGNORED_LABEL)
        # Find emails with Ignored label
        result = service.users().messages().list(
            userId="me", q=f"label:{IGNORED_LABEL}", maxResults=50
        ).execute()
        msgs = result.get("messages", [])
        for m in msgs:
            service.users().messages().modify(
                userId="me", id=m["id"],
                body={"removeLabelIds": [ignored_id]}
            ).execute()
        return {"removed": len(msgs), "message": f"Uklonjeno {IGNORED_LABEL} s {len(msgs)} mejlova. Pokreni /pipeline/run-now."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/gmail/reset-processed")
def gmail_reset_processed():
    """Remove Spedicija-Processed label so emails can be re-fetched."""
    try:
        from googleapiclient.discovery import build
        from google_auth import get_credentials
        from gmail_watcher import PROCESSED_LABEL, _get_or_create_label
        creds = get_credentials()
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        processed_id = _get_or_create_label(service, PROCESSED_LABEL)
        result = service.users().messages().list(
            userId="me", q=f"label:{PROCESSED_LABEL}", maxResults=50
        ).execute()
        msgs = result.get("messages", [])
        for m in msgs:
            service.users().messages().modify(
                userId="me", id=m["id"],
                body={"removeLabelIds": [processed_id]}
            ).execute()
        return {"removed": len(msgs), "message": f"Uklonjeno {PROCESSED_LABEL} s {len(msgs)} mejlova. Pokreni /pipeline/run-now."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Knowledge base import ──────────────────────────────────────────────────
@app.post("/knowledge/import")
def import_declarations(db: Any = Depends(get_db)):
    from xml_parser import load_all_declarations
    from database import TariffRecord
    result = load_all_declarations()
    total = db.query(TariffRecord).count()
    return {
        "message": f"Import završen: {result['files_scanned']} fajlova skeniranih, {result['records']} novih zapisa dodato.",
        "total_records": total,
    }


@app.get("/knowledge/stats")
def knowledge_stats(db: Any = Depends(get_db)):
    from database import TariffRecord, TariffCorrection
    from sqlalchemy import func
    total = db.query(TariffRecord).count()
    corrections = db.query(TariffCorrection).count()
    top_codes = (
        db.query(TariffRecord.tariff_code, func.count(TariffRecord.id).label("n"))
        .group_by(TariffRecord.tariff_code)
        .order_by(func.count(TariffRecord.id).desc())
        .limit(10)
        .all()
    )
    return {
        "tariff_records": total,
        "agent_corrections": corrections,
        "top_codes": [{"code": c, "count": n} for c, n in top_codes],
    }


@app.post("/gmail/reauth")
def gmail_reauth():
    token_path = BASE_DIR / "data" / "token.json"
    if token_path.exists():
        token_path.unlink()
        return {"message": "Token obrisan. Pokreni 'Provjeri Gmail' — otvorit će se browser za prijavu."}
    return {"message": "Token nije postojao — nema šta brisati."}


@app.get("/pipeline/status")
def pipeline_status():
    creds_ok = (BASE_DIR / "credentials.json").exists()
    token_ok = (BASE_DIR / "data" / "token.json").exists()
    job = scheduler.get_job("gmail_poll") if scheduler.running else None
    return {
        "scheduler_running": scheduler.running,
        "credentials_json": creds_ok,
        "token_json": token_ok,
        "next_run": str(job.next_run_time) if job else None,
    }


@app.get("/pipeline/stats")
def pipeline_stats(db: Any = Depends(get_db)):
    from database import PendingShipment, Declaration
    processed = db.query(PendingShipment).filter(PendingShipment.status == "approved").count()
    skipped   = db.query(PendingShipment).filter(PendingShipment.status == "skipped").count()
    pending   = db.query(PendingShipment).filter(PendingShipment.status == "pending").count()
    decl_total = db.query(Declaration).count()
    return {
        "processed": processed,
        "skipped": skipped,
        "pending": pending,
        "declarations_total": decl_total,
    }


# ── XML declarations list & read ─────────────────────────────────────────
@app.get("/output/list")
def list_output_files():
    files = []
    for f in sorted(OUTPUT_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.suffix in (".xml", ".xlsx"):
            files.append({
                "filename": f.name,
                "size": f.stat().st_size,
                "modified": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "type": f.suffix[1:],
            })
    return files


@app.get("/output/xml/{filename}")
def get_xml_content(filename: str):
    import xml.etree.ElementTree as ET

    filepath = OUTPUT_DIR / filename
    if not filepath.exists():
        raise HTTPException(404, "File not found")
    if not filename.endswith(".xml"):
        raise HTTPException(400, "Not an XML file")

    tree = ET.parse(str(filepath))
    root = tree.getroot()

    def txt(el, tag, default=""):
        found = el.find(tag)
        return found.text.strip() if found is not None and found.text else default

    prop = root.find("Property") or ET.Element("_")
    ident = root.find("Identification") or ET.Element("_")
    traders = root.find("Traders") or ET.Element("_")
    consignee_el = traders.find("Consignee") or ET.Element("_")
    exporter_el = traders.find("Exporter") or ET.Element("_")
    valuation = root.find("Valuation") or ET.Element("_")
    container = root.find("Container") or ET.Element("_")

    items = []
    for item_el in root.findall("Item"):
        tarif = item_el.find("Tarification") or ET.Element("_")
        hscode = tarif.find("HScode") or ET.Element("_")
        goods = item_el.find("Goods_description") or ET.Element("_")
        val_item = item_el.find("Valuation_item") or ET.Element("_")
        pkg = item_el.find("Packages") or ET.Element("_")
        items.append({
            "item_number": item_el.get("item_number", ""),
            "commodity_code": txt(hscode, "Commodity_code"),
            "procedure_code": txt(tarif, "Procedure_code"),
            "item_price": txt(tarif, "Item_price"),
            "currency": txt(tarif, "Item_price_currency_code"),
            "description": txt(goods, "Description_of_goods"),
            "commercial_desc": txt(goods, "Commercial_Description"),
            "country_origin": txt(goods, "Country_of_origin_code"),
            "gross_weight": txt(val_item, "Gross_weight_itm"),
            "net_weight": txt(val_item, "Net_weight_itm"),
            "cif_value": txt(val_item, "CIF_item_value"),
            "packages_number": txt(pkg, "Packages_number"),
            "packages_type": txt(pkg, "Packages_type"),
        })

    return {
        "filename": filename,
        "declaration_type": txt(prop, "Declaration_type"),
        "office_code": txt(prop, "Office_code"),
        "procedure_code": txt(prop, "General_procedure_code") + txt(prop, "Extended_procedure_code"),
        "items_count": txt(prop, "Items_count"),
        "registration_date": txt(ident, "Registration_date"),
        "declarant_ref": txt(ident, "Declarant_reference_number"),
        "consignee_name": txt(consignee_el, "Name"),
        "consignee_address": txt(consignee_el, "Address"),
        "consignee_country": txt(consignee_el, "Country_code"),
        "exporter_name": txt(exporter_el, "Name"),
        "exporter_country": txt(exporter_el, "Country_code"),
        "total_value": txt(valuation, "Total_CIF_value"),
        "currency": txt(valuation, "Total_CIF_currency_code"),
        "gross_weight": txt(valuation, "Gross_weight"),
        "net_weight": txt(valuation, "Net_weight"),
        "transport_mode": txt(valuation, "Transport_mode_code"),
        "container_number": txt(container, "Container_number"),
        "packages_number": txt(container, "Packages_number"),
        "packages_type": txt(container, "Packages_type"),
        "items": items,
    }


# ── Detection audit log ────────────────────────────────────────────────────

@app.get("/detection/log")
def get_detection_log(limit: int = 100, db: Any = Depends(get_db)):
    """Return recent detection decisions ordered by ts desc."""
    import json as _json
    from database import DetectionLog
    rows = (
        db.query(DetectionLog)
        .order_by(DetectionLog.ts.desc())
        .limit(limit)
        .all()
    )
    result = []
    for r in rows:
        try:
            att_list = _json.loads(r.attachment_names) if r.attachment_names else []
        except Exception:
            att_list = []
        result.append({
            "id": r.id,
            "ts": r.ts.isoformat() if r.ts else None,
            "gmail_message_id": r.gmail_message_id,
            "subject": r.subject,
            "sender": r.sender,
            "attachment_names": att_list,
            "decision": r.decision,
            "reason": r.reason,
            "stage": r.stage,
        })
    return result


@app.post("/detection/requeue/{gmail_message_id}")
def requeue_detection(gmail_message_id: str, db: Any = Depends(get_db)):
    """Re-run ingestion for a falsely-rejected email."""
    from database import PendingShipment
    # Guard: an already-ingested message must not be re-run (would re-Ignore + double-log).
    if db.query(PendingShipment).filter_by(gmail_message_id=gmail_message_id).first():
        raise HTTPException(status_code=409, detail="Email je već obrađen kao pošiljka.")

    try:
        from googleapiclient.discovery import build
        from google_auth import get_credentials
        from gmail_watcher import (
            IGNORED_LABEL, PENDING_LABEL, PROCESSED_LABEL,
            _get_or_create_label, _ingest_message, UPLOADS_DIR,
        )

        creds = get_credentials()
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)

        # Remove Ignored label so the message is no longer filtered out
        ignored_id   = _get_or_create_label(service, IGNORED_LABEL)
        pending_id   = _get_or_create_label(service, PENDING_LABEL)
        processed_id = _get_or_create_label(service, PROCESSED_LABEL)

        service.users().messages().modify(
            userId="me", id=gmail_message_id,
            body={"removeLabelIds": [ignored_id]}
        ).execute()

        label_ids = {
            "pending":   pending_id,
            "processed": processed_id,
            "ignored":   ignored_id,
        }

        def _cb(msg, level="info"):
            getattr(__import__("logging").getLogger("requeue"), level)(msg)

        try:
            # Fetch the full message and re-run ingestion
            message = service.users().messages().get(
                userId="me", id=gmail_message_id, format="full"
            ).execute()
            decision = _ingest_message(service, message, db, label_ids, _cb)
        except Exception:
            # Ingestion failed after label removal — restore Ignored so the email
            # is not left label-less and invisible to the next poll.
            try:
                service.users().messages().modify(
                    userId="me", id=gmail_message_id,
                    body={"addLabelIds": [ignored_id]}
                ).execute()
            except Exception:
                pass
            raise

        return {"decision": decision, "message": f"Requeue završen: {decision}"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Claude AI classify endpoint ───────────────────────────────────────────
class ClassifyRequest(BaseModel):
    items: list[ItemModel]


@app.post("/classify")
def classify_with_claude(req: ClassifyRequest, db: Any = Depends(get_db)):
    from claude_classifier import classify_items_batch
    items_dicts = [item.dict() for item in req.items]
    return classify_items_batch(items_dicts, db=db)


# ── Declaration AI Chat ───────────────────────────────────────────────────
class ChatMessage(BaseModel):
    role: str   # "user" | "assistant"
    content: str

class DeclarationChatRequest(BaseModel):
    message: str
    items: list[dict]
    metadata: dict
    history: list[ChatMessage] = []


@app.post("/pipeline/chat")
async def declaration_chat(req: DeclarationChatRequest):
    """AI chat that can read and modify the current declaration."""
    import anthropic, json as _json

    client = anthropic.Anthropic()

    # Build declaration context
    items_lines = []
    for i, it in enumerate(req.items, 1):
        name = it.get("name", "")
        qty  = it.get("quantity", 0)
        val  = it.get("value", 0)
        tc   = it.get("tariff_code", "—")
        gw   = it.get("gross_weight", 0)
        items_lines.append(f"  {i}. {name} | {qty} kom | {val} EUR | {gw} kg | Tarifa: {tc}")
    items_text = "\n".join(items_lines) if items_lines else "  (nema stavki)"

    meta = req.metadata
    meta_lines = [
        f"Primatelj: {meta.get('consignee','')}",
        f"PIB: {meta.get('consignee_pib','')}",
        f"Faktura: {meta.get('invoice_number','')}",
        f"Izvoznik: {meta.get('exporter_name','')} / {meta.get('exporter_city','')}",
        f"Kontejner: {meta.get('container_number','')}",
        f"Valuta: {meta.get('currency','EUR')} @ {meta.get('currency_rate','')}",
        f"Isporuka: {meta.get('delivery_terms','')} {meta.get('delivery_place','')}",
        f"Vozarina: {meta.get('external_freight','')} {meta.get('external_freight_currency','')}",
        f"EUR.1: {meta.get('eur1_reference','')}",
    ]
    meta_text = "\n".join(f"  {l}" for l in meta_lines if l.split(": ",1)[-1].strip())

    system_prompt = f"""Ti si AI asistent za carinske deklaracije u Bosni i Hercegovini.
Pomažeš carinskom agentu da provjeri, ispravi i optimizuje deklaraciju.

TRENUTNA DEKLARACIJA
====================
STAVKE:
{items_text}

PODACI DEKLARACIJE:
{meta_text}

UPUTE:
- Odgovaraj na bosanskom jeziku, kratko i jasno.
- Ako korisnik traži izmjenu (promjena naziva, tarifnog broja, količine, vrijednosti,
  metapodataka i sl.) — napravi je i vrati ažurirano stanje.
- Ako korisnik samo pita ili komentariše — odgovori tekstom, items i metadata vrati null.
- Tarifni brojevi su 8-cifreni (BiH standard), validni za ASYCUDA World.
- Za svaku izmjenu stavki vrati KOMPLETAN array items (ne samo izmjenjene).
- Za metadata vrati SAMO izmjenjene ključeve (ne cijeli objekt).

Vrati UVIJEK ovaj JSON format (bez markdowna):
{{
  "reply": "Poruka korisniku",
  "items": null,
  "metadata": null
}}
ili ako ima izmjena:
{{
  "reply": "Opisao što sam promijenio",
  "items": [... kompletna lista stavki ...],
  "metadata": {{"ključ": "nova vrijednost"}}
}}"""

    # Build conversation history for Claude
    messages = []
    for h in req.history[-10:]:  # last 10 messages for context
        messages.append({"role": h.role, "content": h.content})
    messages.append({"role": "user", "content": req.message})

    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=system_prompt,
            messages=messages,
        )
        from database import log_api_usage
        log_api_usage(None, "claude-sonnet-4-6", "chat_declaration", resp.usage)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Chat greška: {e}")

    raw = resp.content[0].text.strip()
    # Strip markdown code blocks if Claude wraps the JSON
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json\n"):
            raw = raw[5:]
        raw = raw.strip().rstrip("```").strip()

    try:
        result = _json.loads(raw)
    except Exception:
        result = {"reply": raw, "items": None, "metadata": None}

    return {
        "reply":    result.get("reply", ""),
        "items":    result.get("items"),
        "metadata": result.get("metadata"),
    }


# ── Tariff Corrections ────────────────────────────────────────────────────
class CorrectionRequest(BaseModel):
    item_name: str
    tariff_code: str
    country_origin: str = "CN"
    source: str = "manual"


@app.post("/tariffs/correct")
def save_correction(req: CorrectionRequest, db: Any = Depends(get_db)):
    from database import OfficialTariff, TariffCorrection
    from claude_classifier import _normalize_name, _clean_code

    normalized = _normalize_name(req.item_name)
    code = _clean_code(req.tariff_code)

    if not normalized or len(code) < 6:
        raise HTTPException(400, "Naziv i tarifni broj su obavezni (min 6 cifara)")

    ot = db.query(OfficialTariff).filter(OfficialTariff.code.like(f"{code}%")).first()
    official_desc = ot.description_bs if ot else ""

    existing = db.query(TariffCorrection).filter(
        TariffCorrection.item_name_normalized == normalized
    ).first()

    from tariff_learning_utils import feed_learning_stores as _vs_upsert

    if existing:
        if existing.tariff_code == code:
            existing.confirmations += 1
            existing.updated_at = datetime.utcnow()
            db.commit()
            _vs_upsert(req.item_name, code, existing.official_desc or official_desc)
            return {"action": "reinforced", "confirmations": existing.confirmations,
                    "tariff_code": code, "item_name": req.item_name}
        else:
            old_code = existing.tariff_code
            existing.tariff_code = code
            existing.official_desc = official_desc
            existing.country_origin = req.country_origin
            existing.confirmations = 1
            existing.source = req.source
            existing.updated_at = datetime.utcnow()
            db.commit()
            _vs_upsert(req.item_name, code, official_desc)
            return {"action": "updated", "old_code": old_code, "tariff_code": code,
                    "item_name": req.item_name, "official_desc": official_desc}
    else:
        from database import TariffCorrection as TC
        corr = TC(
            item_name_normalized=normalized,
            item_name_original=req.item_name.strip(),
            tariff_code=code,
            official_desc=official_desc,
            country_origin=req.country_origin,
            source=req.source,
            confirmations=1,
        )
        db.add(corr)
        db.commit()
        _vs_upsert(req.item_name, code, official_desc)
        return {"action": "created", "tariff_code": code,
                "item_name": req.item_name, "official_desc": official_desc}


@app.get("/tariffs/semantic-search")
def semantic_tariff_search(
    q: str = Query(..., min_length=2),
    chapter: str = Query("", description="Optional 2-digit chapter filter"),
    n: int = Query(10, le=30),
):
    """Semantic vectorstore search — much better than SQL LIKE for short/translated names."""
    from tariff_vectorstore import search as vs_search
    results = vs_search(q, chapter=chapter or None, n=n)
    return results


@app.post("/tariffs/rebuild-vectorstore")
def rebuild_vectorstore_endpoint():
    """Force full rebuild of ChromaDB tariff vector index. Takes ~30-60s."""
    try:
        from tariff_vectorstore import rebuild
        count = rebuild()
        return {"status": "ok", "indexed_codes": count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tariffs/rebuild-keyword-map")
def rebuild_keyword_map_endpoint():
    """Ponovo izgradi keyword_tariff_map iz svih historijskih deklaracija."""
    try:
        import subprocess, sys
        script = str(BASE_DIR / "scripts" / "extract_keyword_tariffs.py")
        result = subprocess.run(
            [sys.executable, script, "--reset"],
            capture_output=True, text=True, cwd=str(BASE_DIR), timeout=60,
        )
        lines = (result.stdout + result.stderr).strip().splitlines()
        # Izvuci statistiku iz ispisa
        stats_lines = [l for l in lines if l.startswith("[")]
        return {"status": "ok", "output": stats_lines}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tariffs/bulk-correct")
def bulk_correct(req: list[dict], db: Any = Depends(get_db)):
    """Save multiple tariff corrections at once. Each: {item_name, tariff_code}"""
    from database import OfficialTariff, TariffCorrection
    from claude_classifier import _normalize_name, _clean_code
    results = []
    for entry in req:
        item_name = entry.get("item_name", "").strip()
        tariff_code = entry.get("tariff_code", "").strip()
        if not item_name or not tariff_code:
            continue
        normalized = _normalize_name(item_name)
        code = _clean_code(tariff_code)
        if len(code) < 6:
            continue
        ot = db.query(OfficialTariff).filter(OfficialTariff.code.like(f"{code}%")).first()
        official_desc = ot.description_bs if ot else ""
        existing = db.query(TariffCorrection).filter(
            TariffCorrection.item_name_normalized == normalized
        ).first()
        if existing:
            existing.tariff_code = code
            existing.official_desc = official_desc
            existing.confirmations += 1
            existing.updated_at = datetime.utcnow()
        else:
            db.add(TariffCorrection(
                item_name_normalized=normalized,
                item_name_original=item_name,
                tariff_code=code,
                official_desc=official_desc,
                confirmations=1,
                source="manual_bulk",
            ))
        results.append({"item_name": item_name, "tariff_code": code, "official_desc": official_desc})
    db.commit()
    return {"saved": len(results), "corrections": results}


@app.get("/tariffs/corrections")
def list_corrections(q: str = Query(""), db: Any = Depends(get_db)):
    from database import TariffCorrection
    query = db.query(TariffCorrection)
    if q:
        query = query.filter(
            TariffCorrection.item_name_normalized.like(f"%{q.upper()}%") |
            TariffCorrection.tariff_code.like(f"{q}%")
        )
    rows = query.order_by(TariffCorrection.confirmations.desc(),
                          TariffCorrection.updated_at.desc()).all()
    return [
        {
            "id": r.id,
            "item_name": r.item_name_original,
            "tariff_code": r.tariff_code,
            "official_desc": r.official_desc or "",
            "country_origin": r.country_origin or "",
            "confirmations": r.confirmations,
            "source": r.source,
            "updated_at": str(r.updated_at)[:16],
        }
        for r in rows
    ]


@app.delete("/tariffs/corrections/{correction_id}")
def delete_correction(correction_id: int, db: Any = Depends(get_db)):
    from database import TariffCorrection
    row = db.query(TariffCorrection).filter_by(id=correction_id).first()
    if not row:
        raise HTTPException(404, "Korekcija nije pronađena")
    db.delete(row)
    db.commit()
    return {"message": "Korekcija obrisana"}


@app.get("/tariffs/corrections/stats")
def correction_stats(db: Any = Depends(get_db)):
    from database import TariffCorrection
    total = db.query(TariffCorrection).count()
    high_conf = db.query(TariffCorrection).filter(TariffCorrection.confirmations >= 3).count()
    return {
        "total_corrections": total,
        "high_confidence": high_conf,
        "coverage_estimate": f"{min(total * 3, 100)}%",
    }


# ── App Settings ──────────────────────────────────────────────────────────
_SETTINGS_KEYS = [
    "declarant_code", "declarant_name", "declarant_ref",
    "office_code", "office_name", "gmail_filter", "notify_email",
]


@app.get("/settings")
def get_settings(db: Any = Depends(get_db)):
    from database import get_setting
    return {k: get_setting(db, k) for k in _SETTINGS_KEYS}


@app.put("/settings")
def update_settings(payload: dict, db: Any = Depends(get_db)):
    from database import set_setting
    saved = {}
    for k, v in payload.items():
        if k in _SETTINGS_KEYS:
            set_setting(db, k, str(v) if v is not None else "")
            saved[k] = v
    return {"saved": saved}


# ── Currency rate ─────────────────────────────────────────────────────────
@app.get("/currency/rate")
def get_currency_rate(currency: str = Query("EUR")):
    from currency_api import fetch_rate
    rate = fetch_rate(currency.upper())
    return {"currency": currency.upper(), "bam_rate": rate, "source": "CBBiH" if rate else "fallback"}


# ── Health & Status ────────────────────────────────────────────────────────
@app.get("/health")
def health(db: Any = Depends(get_db)):
    from database import TariffRecord, Declaration
    tariff_count = db.query(TariffRecord).count()
    decl_count = db.query(Declaration).count()
    return {"status": "ok", "tariff_records": tariff_count, "declarations": decl_count}


@app.get("/system/status")
def system_status():
    return {"ready": _system_ready}


# ── Chat assist (AI carinski asistent za dashboard) ────────────────────────
class ChatAssistRequest(BaseModel):
    messages: list[dict]
    shipment_id: int | None = None
    context: str | None = None

@app.post("/chat/assist")
def chat_assist(req: ChatAssistRequest):
    import anthropic
    client = anthropic.Anthropic()

    system = (
        "Ti si carinski asistent specijalizovan za BiH Carinsku tarifu 2026. "
        "Pomažeš agentu s klasifikacijom robe, tarifnim brojevima, prijevodima "
        "robnih opisa i carinskim procedurama. Odgovori kratko i precizno. "
        "Uvijek navedi tarifni broj u formatu XXXX XX XX XX."
    )
    if req.context:
        system += f"\n\nTRENUTNA DEKLARACIJA:\n{req.context}"

    # Keep last 10 messages to limit tokens
    msgs = req.messages[-10:]

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            messages=msgs,
        )
        from database import log_api_usage
        log_api_usage(None, "claude-haiku-4-5-20251001", "chat_assist", resp.usage)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Chat greška: {e}")

    reply = resp.content[0].text if resp.content else ""
    return {"reply": reply}


# ── Usage stats ────────────────────────────────────────────────────────────
@app.get("/usage/stats")
def usage_stats(db: Any = Depends(get_db)):
    from database import ApiUsage
    from sqlalchemy import func
    from datetime import date, timedelta, time

    today_start = datetime.combine(date.today(), time.min)
    week_start  = datetime.combine(date.today() - timedelta(days=6), time.min)

    def _agg(rows):
        cost = 0.0
        inp  = 0
        out  = 0
        by_model: dict = {}
        for r in rows:
            cost += r.cost_est or 0
            inp  += r.input_tokens or 0
            out  += r.output_tokens or 0
            m = r.model or "unknown"
            if m not in by_model:
                by_model[m] = {"cost": 0.0, "input_tokens": 0, "output_tokens": 0}
            by_model[m]["cost"]          += r.cost_est or 0
            by_model[m]["input_tokens"]  += r.input_tokens or 0
            by_model[m]["output_tokens"] += r.output_tokens or 0
        return {"cost": round(cost, 6), "input_tokens": inp, "output_tokens": out, "by_model": by_model}

    all_rows   = db.query(ApiUsage).all()
    today_rows = db.query(ApiUsage).filter(ApiUsage.ts >= today_start).all()
    week_rows  = db.query(ApiUsage).filter(ApiUsage.ts >= week_start).all()

    return {
        "today":   _agg(today_rows),
        "last_7d": _agg(week_rows),
        "total":   _agg(all_rows),
    }


# ── Dashboard summary ──────────────────────────────────────────────────────
@app.get("/dashboard/summary")
def dashboard_summary(db: Any = Depends(get_db)):
    from database import (
        PendingShipment, Declaration, TariffRecord, TariffCorrection, ApiUsage,
    )
    from sqlalchemy import func
    from datetime import date, timedelta, time

    # Queue counters
    pending   = db.query(PendingShipment).filter_by(status="pending").count()
    extracted = db.query(PendingShipment).filter_by(status="extracted").count()
    uploaded  = db.query(PendingShipment).filter_by(status="uploaded").count()
    approved  = db.query(PendingShipment).filter_by(status="approved").count()
    skipped   = db.query(PendingShipment).filter_by(status="skipped").count()

    # Knowledge base
    tariff_records     = db.query(TariffRecord).count()
    agent_corrections  = db.query(TariffCorrection).count()
    high_conf_corr     = db.query(TariffCorrection).filter(TariffCorrection.confirmations >= 3).count()
    declarations_total = db.query(Declaration).count()

    # Suspect / empty-code items across all extracted+uploaded shipments
    suspect_count = 0
    empty_codes   = 0
    try:
        import json as _json
        from classification_utils import count_suspects
        rows = db.query(PendingShipment).filter(
            PendingShipment.status.in_(["extracted", "uploaded", "approved"])
        ).all()
        for r in rows:
            items = _json.loads(r.extracted_items_json or "[]")
            if items:
                sc, _ = count_suspects(items)
                suspect_count += sc
                empty_codes   += sum(1 for i in items if not (i.get("tariff_code") or "").strip())
    except Exception:
        pass

    # Usage summary (today)
    today_start = datetime.combine(date.today(), time.min)
    today_rows  = db.query(ApiUsage).filter(ApiUsage.ts >= today_start).all()
    usage_today_cost = round(sum(r.cost_est or 0 for r in today_rows), 6)
    usage_today_calls = len(today_rows)

    week_start = datetime.combine(date.today() - timedelta(days=6), time.min)
    week_rows  = db.query(ApiUsage).filter(ApiUsage.ts >= week_start).all()
    usage_7d_cost = round(sum(r.cost_est or 0 for r in week_rows), 6)

    return {
        "queue": {
            "pending":   pending,
            "extracted": extracted,
            "uploaded":  uploaded,
            "approved":  approved,
            "skipped":   skipped,
        },
        "knowledge": {
            "tariff_records":    tariff_records,
            "agent_corrections": agent_corrections,
            "high_conf_corrections": high_conf_corr,
            "declarations_total": declarations_total,
        },
        "quality": {
            "suspect_items": suspect_count,
            "empty_codes":   empty_codes,
        },
        "usage": {
            "today_cost_usd":  usage_today_cost,
            "today_calls":     usage_today_calls,
            "last_7d_cost_usd": usage_7d_cost,
        },
    }
