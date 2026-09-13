"""
Semantic tariff search using ChromaDB + multilingual sentence embeddings.
Model: paraphrase-multilingual-MiniLM-L12-v2 (supports Bosnian/Croatian/Serbian).

Build happens once on first use (~30s), then cached on disk.
"""
import logging
import sys
from pathlib import Path

logger = logging.getLogger("tariff_vectorstore")

CHROMA_PATH = str(Path(__file__).parent.parent / "data" / "chroma_tariff")
COLLECTION_NAME = "tariff_bs_v4"
EMBED_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"

_collection = None

# Keyword enrichment for chapter 95 subcodes.
# Short queries like "IGRAČKA KAMION" won't semantically match
# "ostalo — Tricikli; romobili..." without extra context.
_CH95_KEYWORDS: dict[str, str] = {
    "9503001000": "igračka tricikl romobil automobil pedala bicikl kolica za lutke kolica beba",
    "9503002100": "igračka lutka beba doll tijelo glava dijelovi",
    "9503002900": "igračka lutka beba doll odjeća dodaci pribor",
    "9503003000": "igračka električni vlak voz lokomotiva šine tračnice",
    "9503003500": "igračka kocke konstruktor lego set slaganje građevina",
    "9503003900": "igračka kocke konstruktor lego ostalo građevinski set",
    "9503004100": "igračka životinja meda pas mačka dinosaur zmaj figura",
    "9503004900": "igračka životinja napuhljiv konj ostalo figura neljudski",
    "9503005500": "igračka životinja figura neljudski ostalo",
    "9503006100": "igračka puzzle slagalica mozaik jigsaw podna puzzle djeca",
    "9503006900": "igračka puzzle slagalica mozaik ostalo",
    "9503007000": "igračka puzzle slagalica mozaik ostalo",
    "9503007500": "igračka auto akumulator baterija na daljinski rc car motocikl skuter motor električni model",
    "9503007900": "igračka model motor elektrika ostalo smanjeni model električni",
    "9503008100": "igračka muzički instrument gitara klavir bubnjevi zvuk",
    "9503008500": "igračka ostalo plastika figura",
    "9503008700": "igračka ostalo tekstil plišana figura",
    "9503009500": "igračka kamion automobil auto motorka motocikl avion bager traktor brod robot vojnik "
                 "kuhinja alat pištolj plastična igračka drvena metalna igračka za djecu "
                 "igračka vozilo igračka set igračka dječija society game",
    "9503009900": "igračka ostale razne igračke djeca zabava",
    "9504200000": "video igra konzola kontroler igraća konzola playstation xbox nintendo",
    "9504500000": "igre video konzola igraća konzola djeca elektronska igra",
}


# Keyword enrichment for ch87 — auto parts with Turkish/Bosnian vernacular names
# ASYCUDA descriptions like "ostali dijelovi i pribor" don't match "AMORTIZER" or "FILTER ULJA"
_CH87_KEYWORDS: dict[str, str] = {
    "8708291000": "amortizer amortisör ön amortisör arka amortizor prednji stražnji shock absorber",
    "8708299000": "amortizer amortisör gazlı plinski amortizer koltuk amortisör ostali dijelovi vješanje",
    "8708301000": "kočnica kočioni disk kočioni bubanj papuča fren disk freni rotordar tahir brake",
    "8708309900": "kočnica čeljust kalib kaliper sistem kočenja bremse brake caliper",
    "8708319900": "kočni disk disk kočnice rotordar tahir brake disc rotor",
    "8708401000": "mjenjač menjalnik getriebe gearbox transmisija transmission",
    "8708500000": "osovina most kardan arbre de transmission drive shaft kardanwelle",
    "8708701000": "točkovi naplatci felge felná alaşım jant kotači wheels rims aluminium",
    "8708709100": "točkovi kotači felge čelični steel wheels disk",
    "8708800000": "ovjjes amortizer opruga opruge makazabugri yay makas fjeder suspension spring",
    "8708910000": "hladnjak radijator radyatör kühlwasser coolant radiator wasser",
    "8708921000": "prigušivač ispuha lonac ispuha auspuh egzoz susturucu egzoz boru muffler exhaust silencer",
    "8708929900": "ispušna cijev egzoz borusu ispuh auspuff cijev exhaust pipe",
    "8708940000": "upravljač volani volan upravljanje direksiyon lenker steering wheel column rack",
    "8708951000": "sigurnosni jastuk airbag zracna blazina airbag srs jastuk za sigurnost",
    "8708993500": "brisač vjetrobrana silecek wischer windshield wiper",
    "8708994500": "poklopac retrovizora ayna kapağı spiegel seitenspiegels rückspiegel side mirror cover",
    "8708995000": "gumena traka vrata kapı tozu lastik dichtung door seal rubber",
    "8708997000": "brava vrata kapı kilidi türschloss door lock handle",
    "8708999700": "filter ulja yağ filtresi ölfilter oil filter filter zraka hava filtresi luftfilter air filter",
    "8714910000": "okvir bicikla bike frame fahrradrahmen bicikl okvir",
    "8714990000": "dijelovi pribor bicikla mopeda skutera scooter parts accessories",
}

# Keyword enrichment for ch84 — most common machinery/mechanical items
_CH84_KEYWORDS: dict[str, str] = {
    "8413300000": "pumpa goriva benzinska pumpa yakıt pompası benzinpumpe fuel pump petrol",
    "8413500000": "pumpa rashladna rashladna tekućina water pump soğutma pompası kühlwasserpumpe cooling",
    "8413700000": "centrifugalna pumpa pumpa za vodu kreiselpumpe centrifugal water pump",
    "8414300000": "kompresor klima kompresör klimakompressor ac compressor air conditioning",
    "8414800000": "ventilator fan lüfter kühlventilator electric fan cooling fan",
    "8415100000": "klima uređaj air conditioning ac klima klimaanlage split klima",
    "8421230000": "filter ulja yağ filtresi ölfilter oil filter maziva lubricant",
    "8421310000": "filter zraka hava filtresi luftfilter air filter intake",
    "8421390000": "filter goriva yakıt filtresi kraftstofffilter fuel filter gorivo benzin diesel",
    "8482100000": "ležaj kuglični kugellager bearing roulement rodamiento valjkasti",
    "8483400000": "zupčanik zupčasta letva prijenosnik gear getriebe engrenage",
    "8484100000": "brtva brtvljenje zaptivač tıkama conta dichtung gasket seal",
    "8501200000": "elektromotor jednosmjerni dc motor motor direct current",
    "8501310000": "elektromotor izmjenični ac motor motor jednosmjerni",
    "8516800000": "električna grijalica grijalice električni grijač heizung heater electric",
}

# Keyword enrichment for ch85 — electronics/electrical components
_CH85_KEYWORDS: dict[str, str] = {
    "8507600000": "litij baterija li-ion akumulator punjiva baterija lithium battery rechargeable",
    "8507800000": "punjiva baterija akumulator nikel nicd nimh battery rechargeable",
    "8512200000": "autosignal auto alarm alarm auto kfz car alarm sirena",
    "8512300000": "auto zvučnik hoparlör lautsprecher car speaker audio",
    "8512400000": "brisač vjetrobrana električni motor brisača silecek motoru wischermotor wiper motor",
    "8519810000": "zvučnik bluetooth speaker hoparlör lautsprecher portable",
    "8525800000": "kamera kameralar ip kamera surveillance camera sigurnosna kamera",
    "8536500000": "prekidač električni şalter lichtschalter switch electrical",
    "8544300000": "kabl ožičenje kabelbaum wiring harness kablo demet auto kabl",
    "8544420000": "električni provodnik kabl kabel kablo copper wire conductor",
}

# Keyword enrichment for ch82 — cutlery, knives, scissors, razors (often confused with plastics ch39)
_CH82_KEYWORDS: dict[str, str] = {
    "8211920000": "nož kuhinjski kuhinjski noževi mesarski nož kitchen knife cleaver blade",
    "8212100000": "britva brijač brijanje razor straight razor žilet shaving",
    "8212200000": "žilet oštrica za brijanje razor blade safety razor double edge brijanje",
    "8212100000": "britva za lice brijač muškarac razor shaving face brijanje",
    "8213000000": "makaze škare škarice scissors hair scissors nail scissors cutting",
    "8214200000": "manikir pedikyur set manikir nokatni set pribor za nokte nail care set manicure",
    "8215990000": "kuhinjski pribor lopatica špatula kašika kutlača cooking utensils spatula ladle",
}

# Keyword enrichment for ch33 — cosmetics, perfume, personal care (confused with ch39 plastics)
_CH33_KEYWORDS: dict[str, str] = {
    "3303000000": "parfem miris cologne eau de toilette edt edp parfemska voda fragrance perfume",
    "3304100000": "ruž za usne lip gloss lipstick šminka makeup usne lips",
    "3304200000": "sjenilo za oči maskara eyeliner eye shadow mascara šminka oči",
    "3304300000": "puder manikir lak za nokte nail polish nail varnish lak nokte",
    "3304910000": "puder za lice toner serum foundation powder face makeup",
    "3304990000": "krema za lice moisturizer hidratantna krema kozmetika skin cream face lotion",
    "3305100000": "šampon shampoo hair shampoo kosa frizura",
    "3305200000": "perm za kosu permanent hair color hair dye boja za kosu",
    "3305300000": "lak za kosu hairspray hair gel gel za kosu stilska sredstva styling",
    "3305900000": "balzam za kosu regenerator conditioner hair conditioner njega kose",
    "3307100000": "krema za brijanje gel za brijanje pjena za brijanje shaving cream foam gel",
    "3307200000": "dezodorans deo antiperspirant deodorant roll-on spray",
    "3307300000": "losion nakon brijanja aftershave after shave brijanje",
    "3307900000": "pasta za zube toothpaste denture cleaning teeth oral care",
    "3401110000": "sapun toaletni soap bar soap liquid soap kozmetički sapun",
    "3402200000": "gel za tuširanje gel za kupanje shower gel bath gel washing",
}

# Keyword enrichment for ch61 — knitted clothing (čarape, džemper, hulahopke...)
_CH61_KEYWORDS: dict[str, str] = {
    "6104200000": "dukserica hoodie zip pulover majica pletena sweatshirt",
    "6110200000": "džemper pulover sweater wool cotton jumper knit",
    "6115100000": "čarape hulahopke najlonke stockings tights hosiery",
    "6115960000": "čarape socks cotton ankle crew sport",
    "6107110000": "gaće muške donji veš boxer shorts briefs men underwear",
    "6108110000": "gaće ženske donji veš briefs women panties underwear",
}

# Keyword enrichment for ch62 — woven clothing (košulja, hlače, kravata...)
_CH62_KEYWORDS: dict[str, str] = {
    "6203420000": "muške hlače pantalone trousers men jeans woven",
    "6204620000": "ženske hlače pantalone trousers women jeans woven",
    "6205200000": "muška košulja shirt men cotton woven",
    "6206100000": "ženska bluza blouse shirt women silk woven",
    "6212100000": "grudnjak bra bralette women underwear lingerie",
    "6216000000": "rukavice gloves winter work knit leather",
    "6217100000": "kravata marama šal scarf tie accessories woven",
}

# Keyword enrichment for ch64 — footwear (cipele, patike, sandale, čizme...)
_CH64_KEYWORDS: dict[str, str] = {
    "6402990000": "patike tenisice sportske cipele sneakers rubber sole",
    "6403990000": "kožne cipele leather shoes men women dress",
    "6404110000": "sportske cipele sport shoes textile upper",
    "6404190000": "papuče sandale slippers sandals textile",
    "6405200000": "gumene čizme rubber boots rain boots wellies",
    "6403110000": "skijaške čizme ski boots skiing winter",
}

# Keyword enrichment for ch91 — clocks and watches (sat, budilnik, zidni sat...)
_CH91_KEYWORDS: dict[str, str] = {
    "9101110000": "luksuzni ručni sat luxury watch gold precious metal men wristwatch",
    "9101910000": "ručni sat muški wristwatch men automatic mechanical",
    "9102110000": "ručni sat quartz wristwatch women battery ladies",
    "9103100000": "džepni sat pocket watch chain fob",
    "9105110000": "budilnik alarm clock table clock desk",
    "9105210000": "zidni sat wall clock",
}

# Keyword enrichment for ch42 — leather goods (torba, ruksak, novčanik, kaiš...)
_CH42_KEYWORDS: dict[str, str] = {
    "4202110000": "kožna torba leather handbag woman tote shoulder bag",
    "4202120000": "torba od tekstila fabric bag handbag canvas tote",
    "4202210000": "ruksak backpack school bag student",
    "4202220000": "ruksak putni travel backpack fabric",
    "4202310000": "novčanik kožni leather wallet billfold men",
    "4202320000": "novčanik torbica wallet clutch purse fabric",
    "4202910000": "putna torba travel bag weekend duffel holdall",
    "4205000000": "kaiš remen kožni leather belt strap",
}

# Keyword enrichment for ch49 — printed material (knjiga, novine, časopis...)
_CH49_KEYWORDS: dict[str, str] = {
    "4901100000": "knjiga book hardcover paperback literature novel",
    "4901990000": "knjige books educational textbook learning",
    "4902100000": "novine gazette newspaper daily journal",
    "4902900000": "časopis magazine journal weekly monthly",
    "4911100000": "katalog reklamni advertising catalogue flyer poster",
}

# Keyword enrichment for ch94 — furniture and lighting (stolica, stol, ormar, lampa...)
_CH94_KEYWORDS: dict[str, str] = {
    "9401610000": "stolica tapecirana upholstered chair seat fabric cushion",
    "9401690000": "stolica drvena wooden chair dining room",
    "9401800000": "stolica plastična uredska plastic office chair",
    "9403200000": "stol metalni desk metal table office",
    "9403300000": "stol drveni wooden table dining coffee",
    "9403500000": "polica drvena bookcase shelf wooden storage",
    "9403600000": "ormar namještaj furniture wardrobe cabinet",
    "9404100000": "madrac spring foam mattress bed sleeping",
    "9405100000": "lustera lampa rasvjeta ceiling lamp chandelier light",
    "9405200000": "lampa stolna desk lamp table lamp reading light",
}

# Combined enrichment map — all chapters
_ALL_CHAPTER_KEYWORDS: dict[str, str] = {
    **_CH95_KEYWORDS,
    **_CH87_KEYWORDS,
    **_CH84_KEYWORDS,
    **_CH85_KEYWORDS,
    **_CH82_KEYWORDS,
    **_CH33_KEYWORDS,
    **_CH61_KEYWORDS,
    **_CH62_KEYWORDS,
    **_CH64_KEYWORDS,
    **_CH91_KEYWORDS,
    **_CH42_KEYWORDS,
    **_CH49_KEYWORDS,
    **_CH94_KEYWORDS,
}


# Chapter-level enrichment — applied to EVERY document in the chapter.
# Unlike code-level enrichment, this doesn't depend on exact BiH subcode existence.
# Enables VS-gated chapter forcing for chapters where invoice terms differ from official descriptions.
_CHAPTER_ENRICHMENT: dict[str, str] = {
    "82": "britva brijač britvica makaze nož kuhinjski škare razor knife scissors blade shaving",
    "64": "cipele patike sandale čizme papuče obuća tenisice shoes boots sneakers sandals footwear slippers",
    "42": "torba ruksak novčanik kaiš remen kožna galanterija kofer putna bag handbag wallet belt leather backpack suitcase",
    "91": "sat satovi ručni sat budilnik zidni sat džepni sat watch clock alarm wristwatch timepiece",
    "61": "džemper čarape hulahopke gaće pletena odjeća dukserica hoodie sweater socks tights underwear knit",
    "62": "košulja hlače kravata šal marama rukavice tkana odjeća shirt trousers tie scarf woven clothing",
    "49": "knjiga novine časopis katalog udžbenik štampano book newspaper magazine catalogue printed",
    "94": "stolica stol ormar lampa namještaj sofa krevet polica furniture chair table wardrobe lamp bed shelf",
    "33": "parfem kozmetika šampon krema dezodorans losion gel sapun parfemska perfume cosmetics shampoo cream deodorant lotion",
    "95": "igračka puzzle slagalica guralica prohodalica toy game play children doll puzzle",
    "87": "auto vozilo automobil amortizer kočnica filter dijelovi volani hladnjak car vehicle parts shock absorber brake",
    "84": "pumpa motor kompresor ventilator mašina filter zraka pump compressor engine fan machine air filter",
    "85": "baterija električni elektronika klima kabl alarm kamera battery electric electronics cable camera alarm",
}


def _ef():
    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
    return SentenceTransformerEmbeddingFunction(model_name=EMBED_MODEL)


def _make_doc(code: str, chapter: str, desc: str) -> str:
    """Rich contextual text with two layers of keyword enrichment:
    1. Code-level: specific subcodes with vernacular terms (ch95, ch87, ch84, ch85, ch82, ch33...)
    2. Chapter-level: all documents in a chapter get chapter keywords for reliable VS-gated detection.
    """
    base = f"Tarifni broj {code} poglavlje {chapter} carinska tarifa: {desc}"
    # Code-level enrichment (subcode-specific vernacular terms)
    code_extra = _ALL_CHAPTER_KEYWORDS.get(code.strip(), "")
    if code_extra:
        base += f" | ključne_riječi: {code_extra}"
    # Chapter-level enrichment (applies to ALL codes in this chapter)
    ch_extra = _CHAPTER_ENRICHMENT.get(chapter[:2], "")
    if ch_extra:
        base += f" | poglavlje: {ch_extra}"
    return base


def _build(client, ef):
    logger.info("Gradim tariff vectorstore iz OfficialTariff tabele...")
    sys.stdout.flush()

    col = client.create_collection(COLLECTION_NAME, embedding_function=ef,
                                   metadata={"hnsw:space": "cosine"})

    # Import here to avoid circular imports at module load time
    from database import SessionLocal, OfficialTariff
    db = SessionLocal()
    try:
        rows = db.query(OfficialTariff).all()
        total = len(rows)
        BATCH = 400
        for i in range(0, total, BATCH):
            batch = rows[i:i + BATCH]
            col.add(
                ids=[str(r.id) for r in batch],
                documents=[_make_doc(
                    (r.code or "").strip(),
                    (r.chapter or (r.code or "")[:2] or ""),
                    (r.description_bs or "").strip(),
                ) for r in batch],
                metadatas=[{
                    "code":        (r.code or "").strip(),
                    "chapter":     (r.chapter or (r.code or "")[:2] or ""),
                    "description": (r.description_bs or "")[:300],
                } for r in batch],
            )
            logger.info(f"  Indeksirano {min(i + BATCH, total)}/{total}")
    finally:
        db.close()

    logger.info(f"Tariff vectorstore spreman: {col.count()} kodova")
    return col


def get_collection():
    """Return cached collection, building it on first call."""
    global _collection
    if _collection is not None:
        return _collection

    import chromadb
    ef = _ef()
    client = chromadb.PersistentClient(path=CHROMA_PATH)

    existing = [c.name for c in client.list_collections()]
    if COLLECTION_NAME in existing:
        col = client.get_collection(COLLECTION_NAME, embedding_function=ef)
        if col.count() > 500:
            logger.info(f"Tariff vectorstore učitan: {col.count()} kodova")
            _collection = col
            return _collection
        client.delete_collection(COLLECTION_NAME)

    _collection = _build(client, ef)
    return _collection


def upsert_correction(item_name: str, tariff_code: str, official_desc: str = "") -> None:
    """
    Add or update a single correction entry in the vectorstore.
    Called whenever a TariffCorrection is saved so semantic search stays fresh
    without requiring a full rebuild (~30s).
    """
    col = get_collection()
    # Use lowercase: model produces very different embeddings for ALL CAPS vs lowercase.
    # search() always lowercases queries, so documents must also use lowercase to match.
    doc_text = (
        f"korekcija: {item_name.lower()} → tarifni broj {tariff_code} "
        f"opis: {official_desc.lower()}"
    )
    doc_id = f"corr_{tariff_code}_{item_name[:30].replace(' ', '_').lower()}"
    try:
        col.upsert(
            ids=[doc_id],
            documents=[doc_text],
            metadatas=[{
                "code": tariff_code,
                "chapter": tariff_code[:2],
                "description": f"{item_name} — {official_desc}"[:300],
            }],
        )
        logger.debug(f"Vectorstore upsert: {item_name} → {tariff_code}")
    except Exception as e:
        logger.warning(f"Vectorstore upsert greška: {e}")


def rebuild() -> int:
    """Force full rebuild. Call after OfficialTariff data changes."""
    global _collection
    import chromadb
    ef = _ef()
    client = chromadb.PersistentClient(path=CHROMA_PATH)
    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass
    _collection = _build(client, ef)
    return _collection.count()


def search(query: str, chapter: str = None, n: int = 7) -> list[dict]:
    """
    Semantic search over tariff descriptions.

    Args:
        query:   item name in any language, e.g. "IGRAČKA KAMION"
        chapter: 2-digit chapter string to restrict search, e.g. "95"
        n:       number of results to return

    Returns:
        list of {code, chapter, description, distance} sorted by distance ASC
    """
    col = get_collection()
    count = col.count()
    if count == 0:
        return []

    # Lowercase is critical: paraphrase-multilingual-MiniLM-L12-v2 produces
    # completely different embeddings for ALL CAPS vs lowercase. Since invoice
    # item names arrive in ALL CAPS, always normalise to lowercase before querying.
    query_lc = query.lower()

    where = {"chapter": chapter} if chapter else None
    try:
        results = col.query(
            query_texts=[query_lc],
            n_results=min(n, count),
            where=where,
        )
    except Exception as e:
        logger.warning(f"Vector search greška: {e}")
        return []

    out = []
    if results and results.get("metadatas"):
        for meta, dist in zip(results["metadatas"][0], results["distances"][0]):
            out.append({
                "code":        meta.get("code", ""),
                "chapter":     meta.get("chapter", ""),
                "description": meta.get("description", ""),
                "distance":    round(float(dist), 4),
            })
    return out
