# Spedicija App — Tehnička specifikacija

## Pregled sistema

**Spedicija App** je automatizovani sistem za pripremu carinskih deklaracija za uvoz robe u Bosnu i Hercegovinu. Sistem prima uvozne fakture (PDF ili Excel) i automatski:

1. Čita i razumije dokument pomoću Claude AI
2. Prevodi nazive robe na bosanski jezik
3. Dodjeljuje tačan tarifni broj iz BiH Carinske tarife 2026 (8-cifarni HS kod)
4. Generiše ASYCUDA World XML koji se direktno uvozi u carinski sistem
5. Generiše Excel radni list za internu arhivu i kontrolu
6. Uploaduje sve na Google Drive

Sistem podržava **automatski Gmail pipeline** (bez ikakve intervencije korisnika) i **ručni upload** direktno kroz web interfejs.

---

## Arhitektura

```
┌─────────────────────────────────────────────────────┐
│                   FRONTEND (Browser)                │
│          Single-page app  (index.html)              │
│   Nova deklaracija │ Pipeline │ Ispravci │ Postavke  │
└──────────────┬──────────────────────────────────────┘
               │ HTTP/REST (JSON)
┌──────────────▼──────────────────────────────────────┐
│            BACKEND (FastAPI / Python)               │
│                                                     │
│  main.py          ← REST API, scheduling            │
│  pipeline.py      ← Gmail → AI → Drive              │
│  claude_classifier.py ← Tarifni AI klasifikator     │
│  xml_generator.py ← ASYCUDA XML builder             │
│  excel_generator.py ← Excel izvještaj               │
│  gmail_watcher.py ← Gmail API poller                │
│  drive_uploader.py ← Google Drive upload            │
│  tariff_vectorstore.py ← ChromaDB semantic search   │
│  database.py      ← SQLite ORM modeli               │
└──────────────┬──────────────────────────────────────┘
               │
┌──────────────▼──────────────────────────────────────┐
│                STORAGE & AI                         │
│  SQLite (spedicija.db)   ← sve tabele               │
│  ChromaDB (chroma_tariff/) ← vektorski indeks       │
│  Anthropic Claude API    ← AI analiza + klasifikacija│
│  Gmail API               ← čitanje faktura          │
│  Google Drive API        ← arhiviranje fajlova      │
└─────────────────────────────────────────────────────┘
```

---

## Tech Stack

| Komponenta | Tehnologija |
|---|---|
| Backend framework | FastAPI 0.115 + Uvicorn |
| AI model (ekstrakcija) | Claude Sonnet 4.6 (claude-sonnet-4-6) |
| AI model (Step 1 klasifikacija) | Claude Haiku 4.5 (claude-haiku-4-5-20251001) |
| AI model (Step 2 klasifikacija) | Claude Sonnet 4.6 (claude-sonnet-4-6) |
| Vektorska baza | ChromaDB ≥ 0.4 |
| Embedding model | paraphrase-multilingual-MiniLM-L12-v2 |
| Relaciona baza | SQLite (SQLAlchemy 2.0) |
| PDF čitanje | PyMuPDF (fitz) + pytesseract (OCR fallback) |
| Excel čitanje/pisanje | openpyxl |
| XML generacija | lxml |
| Gmail / Drive | google-api-python-client |
| Scheduler | APScheduler 3.10 |
| Frontend | Vanilla HTML/CSS/JS (single file) |
| OS | macOS (M1/M2) |

---

## Tok podataka — korak po korak

### A) Automatski Gmail pipeline

```
Gmail inbox
   │
   ▼ (svakih 5 min, APScheduler)
gmail_watcher.py
   │  - Traži emailove s PDF/Excel attachment
   │  - Filtrira lokalne fakture (računi za struju, zakup...) po keywords
   │  - Sprema u PendingShipment tabelu (status=pending)
   │  - Dodaje Gmail label "Spedicija-Pending"
   ▼
Pending prikaz u UI (korisnik odobrava ili preskače)
   │
   ▼ (korisnik klikne Odobri)
pipeline.py → _analyze_shipment_with_claude()
   │  - Čita sve attachment fajlove iz jednog emaila
   │  - PDF: pokušaj tekst ekstrakcije (PyMuPDF + OCR)
   │         ako nema dovoljno teksta → Claude Vision (base64 PDF)
   │  - Excel: openpyxl → tekst blok
   │  - Šalje SVE fajlove zajedno u jedan Claude poziv
   │  - Model: Sonnet 4.6 (uvijek — prijevod i ekstrakcija su kompleksni)
   │  - Output: čisti JSON s poljima deklaracije
   ▼
pipeline.py → _auto_assign_tariffs()
   │  - Poziva claude_classifier.classify_items_batch()
   │  - Vraća listu stavki s tariff_code, official_desc, confidence
   ▼
xml_generator.generate_asycuda_xml()
   │  - Konsolidira stavke po tarifnom broju
   │  - Dijeli naimenovanja koja ne staju u polje 31 (3 linije × 55 znakova)
   │  - Generiše ASYCUDA World XML
   ▼
excel_generator.generate_excel()
   │  - Generiše Excel s detaljima deklaracije
   ▼
drive_uploader.upload_declaration_files()
   │  - Kreira folder "Spedicija/[invoice_number]" na Drive-u
   │  - Uploaduje: originalni PDF/Excel, generirani Excel, XML
   ▼
Notifikacija korisniku (email/log)
```

### B) Ručni upload (Nova deklaracija)

```
Korisnik uploaduje PDF/Excel fajl u web UI
   │
   ▼
POST /pipeline/process-files
   │  - Isti tok kao Gmail pipeline
   │  - Vraća JSON s items[], metadata
   ▼
Prikazuje tabelu stavki u UI
   │  - Korisnik može editovati: naziv, tarifni broj, vrijednost, težinu...
   ▼
POST /generate/xml  (ili /generate/excel ili /generate/both)
   │  - Generiše fajl(ove) s trenutnim podacima iz tabele
   ▼
Korisnik preuzima XML i uvozi u ASYCUDA World
```

---

## Modul: Tarifni klasifikator (`claude_classifier.py`)

Najkompleksniji modul — dodjeljuje tačan 8-cifarni HS kod iz BiH Carinske tarife 2026.

### Prioritetni redosljed (po stavci)

```
0. _FORCE_CODE_MAP         — developer-defined direktni override (NAJVIŠI PRIORITET)
   npr. "policijski set" → 9503007000, "podloga saobraćaj" → 5705008000

1. _find_correction()      — human-verified ispravka iz TariffCorrection tabele
   Word-overlap matching + potvrde (confirmations) kao težinski faktor

2. Toy keyword override    — sve stavke s "igr.", "igračka", "lutka", "plišan"...
   → _best_toy_code() bira najspecifičniji 9503 subkod
   npr. "beba na rolama" → 9503002100, "policijski set" → 9503007000

3. Invoice exact           — ako faktura već ima tačan BiH kod (8 cifara)

4. Invoice HS6 match       — prvih 6 cifara iz fakture → nearest BiH code

5. Batch Claude klasifikacija (2-step AI)
```

### AI klasifikacija — 2 koraka

**Step 1 — Odabir poglavlja (Haiku):**
- Šalje sve stavke bez koda zajedno
- Claude bira HS poglavlje (2 cifre) iz 68 poglavlja
- Haiku je brz i jeftin za ovaj korak

**Step 2 — Tačan kod (Sonnet):**
- Za svako poglavlje: filtrira relevantne kodove vektorstoreom (maks 40 od potencijalno 1000+)
- Šalje filtrirane kodove + stavke u jedan Sonnet poziv po poglavlju
- Sonnet bira tačan 8-cifarni kod s confidence oznakom

**Shipment context:**  
Svaki Claude poziv prima listu SVIH ostalih stavki u pošiljci:
```
KONTEKST POŠILJKE: IGR. AUTO, IGR. LUTKA, IGR. POLICIJSKI SET, ...
```
Ovo omogućava zaključivanje: "PODLOGA" u pošiljci punoj igračaka = dječija podloga → ch57, ne ch87.

### Keyword overrides

**`_FORCE_CHAPTER_MAP`** — forsira poglavlje:
- Britva, makaze → ch82 (Metal cutting tools)
- Parfem, šampon, krema → ch33 (Kozmetika)
- Cipele, sandale → ch64 (Obuća)
- Tepih, prostirka, podloga → ch57 (Tekstilne podne obloge)
- ... 15+ kategorija

**`_FORCE_CODE_MAP`** — forsira tačan kod:
- "policijski set", "vatrogasni set" → 9503007000 (Setovi igračaka)
- "igr. beba", "beba lutka" → 9503002100 (Lutke dojenčad)
- "podloga saobraćaj", "prostirka saobraćaj" → 5705008000 (Dječija prostirka)

### Vektorstore (ChromaDB)

- **Model:** paraphrase-multilingual-MiniLM-L12-v2 (podržava bosanski/srpski/hrvatski)
- **Sadržaj:** svi kodovi BiH Carinske tarife 2026 + keyword enrichment za ch95, ch87, ch84, ch85
- **Svrha 1:** filtriranje kandidatnih kodova u Step 2 (umjesto slanja 1000+ kodova → šalje 40)
- **Svrha 2:** VS-gated chapter forcing — ako ≥3 hits u istom poglavlju s distance < 0.35 → forsira to poglavlje (zaobilazi Haiku Step 1)
- **Svrha 3:** Post-validation — low-confidence AI rezultat koji ima VS hit s distance < 0.25 → zamjenjuje

### Self-learning (TariffCorrection tabela)

Svaki put kad korisnik potvrdi/ispravi tarifni broj:
- Sprema se u `tariff_corrections` tabelu
- Upsertuje se u ChromaDB vektorstore
- Sljedeći put ista (ili slična) stavka dobiva ispravljeni kod automatski (word-overlap matching)

---

## Modul: XML Generator (`xml_generator.py`)

Generiše ASYCUDA World XML koji odgovara formatu BiH Carinarnice.

### Konsolidacija naimenovanja (Polje 31)

Stavke s istim tarifnim brojem se konsoliduju u jedno naimenovanje.

**Format Polja 31 (3 linije × 55 znakova):**
```
Linija 1: Opis poglavlja (heading iz tarife)
Linija 2: Naziv stavke 1, Naziv stavke 2, ...
Linija 3: ...nastavak
```

Pravila:
- Heading UVIJEK na liniji 1 (minimum 1 linija, može biti 2 ako je dugačak)
- Ostatak prostora za nazive stavki
- Ako se stavke ne uklapaju → nova naimenovanja s istim heading-om
- Vrijednost i težina proporcionalno raspoređena po naimenovanjima

### Ključna polja ASYCUDA XML

| Polje | Sadržaj |
|---|---|
| 1 | Tip deklaracije (IM4) |
| 2 | Pošiljalac/Primalac |
| 8 | PIB/JIB primatelja |
| 14 | Carinski agent |
| 20 | Uslovi isporuke (CIF) |
| 22 | Valuta i kurs |
| 31 | Opis robe (heading + nazivi stavki) |
| 33 | Tarifni broj (8 cifara) |
| 34 | Zemlja porijekla |
| 35/38 | Bruto/neto težina |
| 41 | Dopunska mjerna jedinica |
| 42 | Vrijednost stavke |
| 44 | Dokumenti (EUR1, transport...) |
| 46 | Statistička vrijednost |
| 47 | Obračun dažbina |

---

## Modul: Pipeline (`pipeline.py`)

### Analiza dokumenta — Claude prompt

Sistem šalje sve fajlove jednog emaila zajedno u jedan Claude poziv s detaljnim uputama:

- **Pravilo naziva robe:** bosanski, VELIKA SLOVA, max 4 riječi; prijevodi za turski/kineski/engleski
- **IZNIMKA:** ako je naziv već na bosanskom/srpskom/hrvatskom latinicom → koristi doslovno
- **Pravilo težina:** uvijek uzimaj TOTAL red, nikad ne računaj iz stavki
- **Pravilo vrijednosti:** uzimaj summary/total red fakture; freight/insurance → posebna polja
- **Prijevod igračaka:** format "IGR. [vrsta]" (IGR. AUTO, IGR. LUTKA, IGR. KOCKE...)

### PDF strategija

```
PDF primljen
   │
   ├─ PyMuPDF tekst ekstrakcija (+ OCR po stranici ako je loš kvalitet)
   │   ├─ Tekst OK (≥100 chars/str + decimalni brojevi) → šalje kao text blok (jeftino)
   │   └─ Nema dovoljno teksta (skenirani PDF) → šalje kao base64 Vision blok (skuplje)
   │
   └─ Claude procesira
```

### Detekcija lokalnih faktura

Gmail watcher filtrira emailove koji NISU carinska deklaracija:
- Ključne riječi: "račun za struju", "faktura za kiriju", "plaćanje", "virman", "uplatnica"...
- Sprječava nepotrebnu obradu lokalnih troškova

---

## Modul: Gmail Watcher (`gmail_watcher.py`)

- Pokreće se svakih 5 minuta (APScheduler)
- Koristi Gmail API s OAuth2 kreditima (`credentials.json`)
- Sprema attachment-e na disk: `data/uploads/[email_id]/`
- Dodjeljuje Gmail label "Spedicija-Pending"
- Duplikat detekcija po SHA256 hash-u svih attachment-a

---

## Modul: Drive Uploader (`drive_uploader.py`)

- Kreira folder "Spedicija" u root-u Google Drivea (ako ne postoji)
- Za svaku deklaraciju kreira subfolder po broju fakture
- Uploaduje: originalni PDF/Excel, generirani Excel, ASYCUDA XML
- Vraća share linkove

---

## Baza podataka (SQLite)

### Tabele

| Tabela | Opis |
|---|---|
| `tariff_records` | Historijske deklaracije — koristi se za RAG pretragu |
| `official_tariffs` | BiH Carinska tarifa 2026 (sve ~5000+ kodova) |
| `declarations` | Arhiv generiranih deklaracija (XML content) |
| `pending_shipments` | Gmail emailovi koji čekaju odobrenje |
| `tariff_corrections` | Human-verified ispravke tarifnih kodova |
| `app_settings` | Konfiguracija (declarant, office, Gmail filter...) |

### TariffCorrection — self-learning mehanizam

```python
class TariffCorrection:
    item_name_normalized  # UPPERCASE, trimmed — za lookup
    item_name_original    # original naziv
    tariff_code           # 8-cifarni kod
    official_desc         # opis iz BiH tarife
    confirmations         # koliko puta potvrđeno (više = veći prioritet)
    source                # 'manual', 'selection', 'auto_generate', 'manual_bulk'
```

Lookup algoritam:
1. Exact match po normalized name
2. Word-overlap match (≥60% zajedničkih riječi), težinski faktor × confirmations

---

## REST API — glavne rute

### Pipeline

| Metoda | Ruta | Opis |
|---|---|---|
| POST | `/pipeline/process-files` | Ručni upload fajlova → AI analiza |
| POST | `/pipeline/approve/{id}` | Odobri pending Gmail email |
| POST | `/pipeline/skip/{id}` | Preskoči pending email |
| GET | `/pipeline/pending` | Lista pending emailova |
| GET | `/pipeline/log` | Pipeline log (zadnjih 100 unosa) |
| POST | `/pipeline/chat` | AI chat za izmjenu deklaracije |

### Generacija

| Metoda | Ruta | Opis |
|---|---|---|
| POST | `/generate/xml` | Generiši ASYCUDA XML |
| POST | `/generate/excel` | Generiši Excel |
| POST | `/generate/both` | Generiši oboje odjednom |
| GET | `/download/{filename}` | Preuzmi generiran fajl |

### Tarifa i klasifikacija

| Metoda | Ruta | Opis |
|---|---|---|
| POST | `/tariffs/classify` | Klasificiraj jednu stavku |
| POST | `/tariffs/classify-batch` | Batch klasifikacija |
| GET | `/tariffs/search` | Pretraži tarifu (SQL LIKE) |
| GET | `/tariffs/semantic-search` | Semantička pretraga (ChromaDB) |
| POST | `/tariffs/upload-official-pdf` | Upload BiH Tarife PDF |
| POST | `/tariffs/corrections` | Spremi jednu ispravku |
| POST | `/tariffs/bulk-correct` | Spremi više ispravki odjednom |
| GET | `/tariffs/corrections` | Lista svih ispravki |
| DELETE | `/tariffs/corrections/{id}` | Obriši ispravku |
| POST | `/tariffs/rebuild-vectorstore` | Ponovo izgradi ChromaDB index |

### Sistem

| Metoda | Ruta | Opis |
|---|---|---|
| GET | `/health` | Status sistema (DB, VS, ispravke) |
| GET | `/settings` | Čitaj konfiguraciju |
| POST | `/settings` | Spremi konfiguraciju |
| GET | `/auth/gmail` | Pokreni Gmail OAuth flow |
| GET | `/auth/drive` | Pokreni Drive OAuth flow |

---

## Frontend — korisničko sučelje

Single-page app s tabovima:

### Nova deklaracija (tab-nova)

**Korak 1 — Upload fajla:**
- Drag & drop ili klik za odabir PDF/Excel
- Podrška za više fajlova odjednom (faktura + packing lista + B/L)

**Korak 2 — Pregled i editovanje:**
- Tabela svih stavki: naziv, količina, vrijednost, težina, tarifni broj, zemlja porijekla
- Inline editing svake ćelije
- Autocomplete za tarifne brojeve s prikaz opisa
- Prikaz confidence (zelena/žuta/crvena)
- Ukupni zbrojevi automatski (vrijednost, težina)

**Korak 3 — Zaglavlje deklaracije:**
- Uvoznik (firma, PIB, adresa)
- Pošiljalac (firma, grad, ulica, zemlja)
- Broj fakture, valuta, kurs, Incoterms
- Kontejner, transport, granični prijelaz, tranzitni dokument
- EUR1 referenca, datum deklaracije

**Korak 4 — Generiši output:**
- Dugme za XML, Excel ili oboje
- Linkovi za preuzimanje generiranih fajlova
- **Potvrdi sve tarifne brojeve** — sprema sve kodove u corrections bazu za buduće automatsko korištenje

**Korak 5 — AI Asistent (chat):**
- Razgovor s Claude AI za izmjenu deklaracije
- Primjeri: "Promijeni tarifni broj za stavku 3", "Provjeri da li su tarifni brojevi tačni"
- AI može mijenjati items[] i metadata i vraća ažurirani state

### Gmail Pipeline (tab-pipeline)

- Prikaz pending emailova s opcijama Odobri/Preskoči
- Live log pipeline aktivnosti
- Status automatskog schedulera

### Ispravci (tab-corrections)

- Pregled svih saved ispravki s brojem potvrda
- Pretraga po imenu stavke ili tarifnom broju
- Brisanje pogrešnih ispravki
- Statistika: ukupno ispravki, visoko-pouzdane (≥3 potvrde)

### Postavke (tab-settings)

- Podaci deklaranta (firma, kod, adresa)
- Gmail filter (koje emailove hvatati)
- Notifikacijski email
- Granični prijelaz i naziv

---

## AI Chat za deklaracije

### Endpoint: `POST /pipeline/chat`

Prijem:
```json
{
  "message": "Tarifni broj za stavku 1 je 9503002100",
  "items": [...],
  "metadata": {...},
  "history": [{"role": "user", "content": "..."}, ...]
}
```

Odgovor:
```json
{
  "reply": "Ažuriram tarifni broj za IGR. LUTKA BEBA na 9503002100.",
  "items": [...],     // ažurirana lista ili null
  "metadata": {...}   // ažurirani metadata ili null
}
```

Claude dobiva kompletan kontekst deklaracije i može:
- Mijenjati tarifne brojeve, nazive, vrijednosti, težine
- Objašnjavati klasifikacijske odluke
- Provjeravati logičnost kodova u kontekstu cijele pošiljke

---

## Konfiguracija i pokretanje

### Potrebni fajlovi

```
/project-root/
├── .env                    # ANTHROPIC_API_KEY=...
├── credentials.json        # Google OAuth2 krediti (Gmail + Drive)
├── backend/
│   └── requirements.txt
└── data/
    ├── spedicija.db        # SQLite (auto-kreira)
    ├── chroma_tariff/      # ChromaDB index (auto-kreira)
    └── output/             # Generirani fajlovi
```

### Pokretanje

```bash
cd backend
./dev.sh           # Development (bez --reload zbog iCloud)
# ili
./start.sh         # Production
```

Pokretanje interno: `uvicorn main:app --host 0.0.0.0 --port 8000`

Aplikacija dostupna na: `http://localhost:8000`

### Environment varijable (.env)

```env
ANTHROPIC_API_KEY=sk-ant-...
```

---

## Optimizacije

### Token caching (Anthropic prompt cache)

- **STATIC_INSTRUCTIONS** blok (pravila prijevoda, format, upute) — označen s `cache_control: ephemeral`
- **Kodovi poglavlja** u Step 2 — označeni kao ephemeral (cache read = 10× jeftiniji)
- Ušteda ~30-40% troškova API poziva

### Retry strategija za duuge fakture

```
max_tokens pokušaji: 4500 → 8000 → 16000
```
Ako Claude trunkira JSON → automatski pokušaj s više tokena.
Ako svi pokušaji ne uspiju → `_repair_truncated_json()` oporavlja djelimični JSON.

### Vektorstore filtriranje kodova

- Chapter 84 ima 1044 kodova (~52k tokena)
- Vektorstore filtrira na ~35 relevantnih (~1500 tokena) — **35× redukcija tokena**
- Fallback: ako VS nađe < 10 relevantnih → šalje sve kodove poglavlja

### PDF strategija

- Tekst PDF → PyMuPDF direktno (brzo i jeftino, nema Vision)
- Skenirani PDF → Claude Vision (skuplje ali neophodan)
- Quality check: ≥100 chars/stranica + decimalni brojevi = tekst PDF

---

## Poznata ograničenja

- ASYCUDA World XML format je specifičan za BiH carinarnice — nije generički
- Polje 31 (opis robe) ograničeno na 3 linije × 55 znakova po naimenovanju
- Automatski Gmail pipeline zahtijeva Google OAuth2 credentials.json
- ChromaDB index se mora ponovo graditi nakon uploada nove BiH tarife
- Tarifni AI nije 100% tačan za neobične ili kratke nazive — preporučuje se pregled tabele prije generisanja XML-a

---

## Dijagram toka klasifikacije

```
Stavka prima na klasifikaciju
         │
         ▼
_FORCE_CODE_MAP?  ──► DA → direktno dodijeli kod (9503007000...)
         │ NE
         ▼
TariffCorrection match? ──► DA → koristi human-verified kod
         │ NE
         ▼
Toy keyword? ──► DA → _best_toy_code() → 9503XXXXXX
         │ NE
         ▼
Faktura ima tačan BiH kod? ──► DA → koristi ga
         │ NE
         ▼
Faktura ima HS6? ──► DA → nađi nearest BiH code po HS6
         │ NE
         ▼
VS gated chapter force? ──► DA → forsira poglavlje, preskoči Haiku
         │ NE
         ▼
Keyword chapter force? ──► DA → forsira poglavlje, preskoči Haiku
         │ NE
         ▼
HAIKU Step 1: odaberi poglavlje iz 68 opcija
         │
         ▼
SONNET Step 2: odaberi tačan kod iz ~40 VS-filtriranih kodova
         │
         ▼
Post-validation: confidence=low + VS hit < 0.25 → zamijeni VS kodom
         │
         ▼
Finalni tarifni broj ✓
```
