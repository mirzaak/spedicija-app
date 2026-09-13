"""Edge-case tests for xml_generator.py ASYCUDA XML generation."""
import pytest
from lxml import etree

import xml_generator as xg


def _parse(xml_str):
    return etree.fromstring(xml_str.encode("utf-8"))


def _text(root, xpath):
    els = root.findall(xpath)
    return [e.text for e in els]


# ── _merge_names / _names_str ────────────────────────────────────────────

def test_merge_names_sums_duplicate_case_insensitive():
    merged = xg._merge_names([("Vijak M6", 2), ("VIJAK M6", 3), ("Matica", 1)])
    assert merged == [("Vijak M6", 5.0), ("Matica", 1.0)]


def test_merge_names_empty_list():
    assert xg._merge_names([]) == []


# ── _wrap_text / _hard_wrap ──────────────────────────────────────────────

def test_wrap_text_empty_string():
    assert xg._wrap_text("") == ""


def test_wrap_text_single_word_longer_than_width_hard_cuts():
    text = "A" * 130
    wrapped = xg._wrap_text(text, width=55)
    lines = wrapped.split("\n")
    assert all(len(l) <= 55 for l in lines)
    assert "".join(lines) == text


def test_hard_wrap_empty_string_returns_one_empty_line():
    assert xg._hard_wrap("", 55) == [""]


def test_hard_wrap_exact_multiple_of_width():
    text = "X" * 110
    lines = xg._hard_wrap(text, 55)
    assert len(lines) == 2
    assert all(len(l) == 55 for l in lines)


# ── _resolve_heading ──────────────────────────────────────────────────────

def test_resolve_heading_empty_falls_back_to_description():
    assert xg._resolve_heading("", "- Neki opis robe") == "Neki opis robe"


def test_resolve_heading_too_short_falls_back():
    assert xg._resolve_heading("ab", "- Opis") == "Opis"


def test_resolve_heading_pure_number_falls_back():
    assert xg._resolve_heading("12345", "- Opis robe") == "Opis robe"


def test_resolve_heading_pure_number_with_dots_and_spaces_falls_back():
    assert xg._resolve_heading("12. 345,00", "- Opis") == "Opis"


def test_resolve_heading_valid_heading_kept():
    assert xg._resolve_heading("Vijci i matice", "- Opis") == "Vijci i matice"


def test_resolve_heading_both_empty_returns_empty():
    assert xg._resolve_heading("", "") == ""


# ── _resolve_description ──────────────────────────────────────────────────

def test_resolve_description_no_db_returns_as_is():
    assert xg._resolve_description("Neki opis", "8544.42.90", None) == "Neki opis"


def test_resolve_description_empty_no_db_no_code_returns_empty():
    assert xg._resolve_description("", "", None) == ""


def test_resolve_description_legacy_em_dash_without_db_falls_through():
    # No db/tariff_code available → keeps legacy text (safety net can't run)
    text = "Kategorija — podkategorija"
    assert xg._resolve_description(text, "", None) == text


# ── _consolidate_by_tariff ──────────────────────────────────────────────

def test_consolidate_merges_same_code():
    items = [
        {"name": "Vijak", "tariff_code": "73181500", "quantity": 2, "value": 10,
         "gross_weight": 1, "net_weight": 0.9},
        {"name": "Podloska", "tariff_code": "73181500", "quantity": 3, "value": 5,
         "gross_weight": 0.5, "net_weight": 0.4},
    ]
    result = xg._consolidate_by_tariff(items)
    assert len(result) == 1
    assert result[0]["quantity"] == 5
    assert result[0]["value"] == 15
    assert result[0]["_names"] == [("Vijak", 2.0), ("Podloska", 3.0)]


def test_consolidate_keeps_items_without_tariff_code_separate():
    items = [
        {"name": "A", "tariff_code": "", "quantity": 1, "value": 1},
        {"name": "B", "tariff_code": "", "quantity": 1, "value": 1},
    ]
    result = xg._consolidate_by_tariff(items)
    assert len(result) == 2


def test_consolidate_never_merges_suspect_items():
    items = [
        {"name": "A", "tariff_code": "73181500", "quantity": 1, "value": 1,
         "confidence": "low"},
        {"name": "B", "tariff_code": "73181500", "quantity": 1, "value": 1,
         "confidence": "low"},
    ]
    result = xg._consolidate_by_tariff(items)
    assert len(result) == 2


def test_consolidate_suspect_does_not_merge_into_existing_confident_group():
    items = [
        {"name": "A", "tariff_code": "73181500", "quantity": 1, "value": 1},
        {"name": "B", "tariff_code": "73181500", "quantity": 1, "value": 1,
         "confidence": "medium"},
    ]
    result = xg._consolidate_by_tariff(items)
    assert len(result) == 2


def test_consolidate_empty_list():
    assert xg._consolidate_by_tariff([]) == []


# ── _split_overflowing_groups ─────────────────────────────────────────────

def test_split_overflow_creates_multiple_naimenovanja():
    names = [(f"Artikl broj {i} dugacko ime proizvoda", 1) for i in range(20)]
    group = {"tariff_code": "73181500", "quantity": 20, "value": 100,
             "gross_weight": 10, "net_weight": 9, "_names": names}
    result = xg._split_overflowing_groups([group])
    assert len(result) > 1
    total_qty = sum(r["quantity"] for r in result)
    assert total_qty == pytest.approx(20, abs=0.01)
    for r in result:
        assert r["_include_heading"] is True


def test_split_overflow_single_chunk_returns_original_group():
    group = {"tariff_code": "1", "quantity": 1, "value": 1,
              "_names": [("Kratko ime", 1)]}
    result = xg._split_overflowing_groups([group])
    assert result == [group]


# ── generate_asycuda_xml: structural edge cases ──────────────────────────

def test_empty_items_list_produces_valid_xml_with_zero_items():
    xml_str = xg.generate_asycuda_xml(items=[])
    root = _parse(xml_str)
    assert root.findall("Item") == []
    assert root.find("Property/Nbers/Total_number_of_items").text == "0"


def test_single_item_minimal_fields():
    items = [{"name": "Test artikl", "tariff_code": "8544429000", "quantity": 1,
              "value": 100, "gross_weight": 1, "net_weight": 0.9}]
    xml_str = xg.generate_asycuda_xml(items=items)
    root = _parse(xml_str)
    assert len(root.findall("Item")) == 1
    hscode = root.find("Item/Tarification/HScode/Commodity_code")
    assert hscode.text == "85444290"


def test_tariff_code_shorter_than_8_digits_is_padded():
    items = [{"name": "X", "tariff_code": "731815", "quantity": 1, "value": 1}]
    xml_str = xg.generate_asycuda_xml(items=items)
    root = _parse(xml_str)
    commodity = root.find("Item/Tarification/HScode/Commodity_code")
    assert commodity.text == "73181500"


def test_tariff_code_with_dots_and_spaces_stripped():
    items = [{"name": "X", "tariff_code": "8544.42 90", "quantity": 1, "value": 1}]
    xml_str = xg.generate_asycuda_xml(items=items)
    root = _parse(xml_str)
    commodity = root.find("Item/Tarification/HScode/Commodity_code")
    assert commodity.text == "85444290"


def test_missing_tariff_code_defaults_to_zeros():
    items = [{"name": "X", "quantity": 1, "value": 1}]
    xml_str = xg.generate_asycuda_xml(items=items)
    root = _parse(xml_str)
    commodity = root.find("Item/Tarification/HScode/Commodity_code")
    assert commodity.text == "00000000"


def test_zero_total_value_avoids_division_by_zero():
    items = [{"name": "A", "tariff_code": "1", "quantity": 1, "value": 0},
             {"name": "B", "tariff_code": "2", "quantity": 1, "value": 0}]
    # Should not raise ZeroDivisionError
    xml_str = xg.generate_asycuda_xml(items=items, internal_freight=10)
    root = _parse(xml_str)
    assert len(root.findall("Item")) == 2


def test_suspect_item_gets_marker_in_marks2_and_free_text():
    items = [{"name": "Dvosmisleno", "tariff_code": "1", "quantity": 1, "value": 1,
              "confidence": "low", "tariff_source": "ai"}]
    xml_str = xg.generate_asycuda_xml(items=items)
    root = _parse(xml_str)
    marks2 = root.find("Item/Packages/Marks2_of_packages")
    assert marks2.text == "POŠILJKA - PROVJERITI TARIFU"
    free_text = root.find("Item/Free_text_1")
    assert free_text.text == "PROVJERITI TARIFU: AI low / ai"


def test_non_suspect_item_no_marker():
    items = [{"name": "Sigurno", "tariff_code": "1", "quantity": 1, "value": 1,
              "confidence": "high"}]
    xml_str = xg.generate_asycuda_xml(items=items)
    root = _parse(xml_str)
    marks2 = root.find("Item/Packages/Marks2_of_packages")
    assert marks2.text == "POŠILJKA"
    free_text_parent = root.find("Item/Free_text_1")
    assert free_text_parent.find("null") is not None


def test_empty_confidence_is_not_suspect():
    items = [{"name": "X", "tariff_code": "1", "quantity": 1, "value": 1,
              "confidence": ""}]
    xml_str = xg.generate_asycuda_xml(items=items)
    root = _parse(xml_str)
    marks2 = root.find("Item/Packages/Marks2_of_packages")
    assert marks2.text == "POŠILJKA"


def test_container_number_sets_flag_true_and_adds_container_blocks():
    items = [{"name": "X", "tariff_code": "1", "quantity": 1, "value": 1,
              "gross_weight": 5}]
    xml_str = xg.generate_asycuda_xml(items=items, container_number="MSKU1234567")
    root = _parse(xml_str)
    assert root.find("Transport/Container_flag").text == "true"
    assert len(root.findall("Container")) == 1


def test_no_container_number_flag_false_no_container_blocks():
    items = [{"name": "X", "tariff_code": "1", "quantity": 1, "value": 1}]
    xml_str = xg.generate_asycuda_xml(items=items)
    root = _parse(xml_str)
    assert root.find("Transport/Container_flag").text == "false"
    assert root.findall("Container") == []


def test_xml_declaration_uses_double_quotes():
    xml_str = xg.generate_asycuda_xml(items=[])
    assert xml_str.startswith('<?xml version="1.0" encoding="UTF-8" standalone="no"?>')


def test_special_characters_in_name_are_escaped_and_parseable():
    items = [{"name": 'Kabl <5m> & "spojnica"', "tariff_code": "1", "quantity": 1,
              "value": 1}]
    xml_str = xg.generate_asycuda_xml(items=items)
    root = _parse(xml_str)  # must not raise
    cd = root.find("Item/Goods_description/Commercial_Description")
    assert "5M" in cd.text
    assert "&" in cd.text


def test_currency_rate_zero_produces_empty_rate_field():
    items = [{"name": "X", "tariff_code": "1", "quantity": 1, "value": 1}]
    xml_str = xg.generate_asycuda_xml(items=items, currency_rate=0.0)
    root = _parse(xml_str)
    rate = root.find("Valuation/Gs_Invoice/Currency_rate")
    assert rate.text is None or rate.text == ""


def test_exporter_with_no_fields_gets_null_element():
    xml_str = xg.generate_asycuda_xml(items=[], exporter_country="")
    root = _parse(xml_str)
    exp_name = root.find("Traders/Exporter/Exporter_name")
    assert exp_name.find("null") is not None


def test_exporter_country_only_still_produces_name_from_country():
    # exporter_country defaults to "CN" — country name alone is enough to
    # populate Exporter_name even with no exporter_name/city/street given.
    xml_str = xg.generate_asycuda_xml(items=[])
    root = _parse(xml_str)
    exp_name = root.find("Traders/Exporter/Exporter_name")
    assert exp_name.text == "KINA"


def test_declaration_date_defaults_to_today_when_missing():
    xml_str = xg.generate_asycuda_xml(items=[], declaration_date="")
    # Should not raise; year_2d derivation shouldn't crash with empty date
    root = _parse(xml_str)
    assert root is not None


def test_eur1_reference_adds_pe1_doc_to_every_item():
    items = [{"name": "A", "tariff_code": "1", "quantity": 1, "value": 1},
             {"name": "B", "tariff_code": "2", "quantity": 1, "value": 1}]
    xml_str = xg.generate_asycuda_xml(items=items, eur1_reference="EUR1-REF-123")
    root = _parse(xml_str)
    pe1_codes = root.findall(".//Attached_document_code")
    pe1_refs = [e.text for c, e in zip(pe1_codes, root.findall(".//Attached_documents"))]
    all_codes = _text(root, ".//Attached_document_code")
    assert all_codes.count("PE1") == 2


def test_turkey_exporter_auto_derives_trpr_preference():
    items = [{"name": "X", "tariff_code": "1", "quantity": 1, "value": 1}]
    xml_str = xg.generate_asycuda_xml(items=items, exporter_country="TR")
    root = _parse(xml_str)
    pref = root.find("Item/Tarification/Preference_code")
    assert pref.text == "TRPR"


def test_many_items_totals_and_item_count_consistent():
    items = [{"name": f"Item{i}", "tariff_code": str(1000 + i), "quantity": 1,
              "value": 10, "gross_weight": 1, "net_weight": 0.9} for i in range(15)]
    xml_str = xg.generate_asycuda_xml(items=items)
    root = _parse(xml_str)
    assert len(root.findall("Item")) == 15
    assert root.find("Property/Nbers/Total_number_of_items").text == "15"
    assert root.find("General_information/Value_details").text == "150.00"
