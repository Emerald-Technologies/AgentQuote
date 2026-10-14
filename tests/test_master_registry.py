import hashlib
import io
import zipfile
from pathlib import Path

import pytest

from bom_processor.master_registry import (
    ApprovalRecord,
    CandidateRecord,
    FixtureBinding,
    LocalRegistryStore,
    MasterRegistry,
    RegistryError,
    VariantSignature,
    profile_bytes,
)


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def csv_bytes() -> bytes:
    return b"Part Number,Description,Quantity\nP1,Widget,2\n"


def xlsx_inline(headers=("Part Number", "Description", "Quantity")) -> bytes:
    cells = "".join(f'<c r="{chr(65 + i)}1" t="inlineStr"><is><t>{value}</t></is></c>' for i, value in enumerate(headers))
    workbook = '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="BOM" sheetId="1" r:id="rId1"/></sheets></workbook>'
    relationships = '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
    sheet = f'<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1">{cells}</row></sheetData></worksheet>'
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return output.getvalue()


def arena_html(description="±25PPM/°C &amp; Â± tolerance") -> bytes:
    return f'''<html><head><meta http-equiv="content-type" content="text/html;charset=UTF-8"><title>Report</title></head><body style="font-family:Arial"><table border="1" cellpadding="0" cellspacing="0"><tr style="font-size:10pt;background:#CCC"><th align="left" valign="bottom">item_number</th><th align="left" valign="bottom">item_name</th><th align="left" valign="bottom">quantity</th></tr><tr style="font-size:10pt; background:transparent" valign="top"><td>P1</td><td>{description}</td><td>1</td></tr></table></body></html>'''.encode("utf-8")


def signature(extension=".csv", sheet="Sheet1"):
    return VariantSignature(frozenset({extension}), frozenset({sheet}), {sheet: frozenset({"part_number", "description", "quantity"})})


def records(code=b"raise SystemExit('must never execute')\n", version="1"):
    sig = signature()
    fixture = FixtureBinding("fixture1", "1" * 64, "2" * 64, "3" * 64)
    candidate = CandidateRecord("candidate1", "Astronics", "customer1", "candidates/candidate1/parse.py", sha(code), sig.signature_hash, sig.rule_hash, (fixture,), "passed", "author1", "2026-09-18T12:00:00Z")
    approval = ApprovalRecord("approval1", candidate.candidate_id, candidate.parser_key, candidate.customer, "format1", version, candidate.candidate_hash, candidate.code_sha256, sig.signature_hash, sig.rule_hash, candidate.fixture_hash, "admin1", "2026-09-18T12:01:00Z")
    return code, sig, candidate, approval


def test_profiles_are_deterministic_and_inline_xlsx_headers_work():
    first = profile_bytes(csv_bytes(), ".csv")
    second = profile_bytes(csv_bytes(), "csv")
    assert first.profile_hash == second.profile_hash
    assert first.header_candidates["Sheet1"][0] == ("part_number", "description", "quantity")
    xlsx = profile_bytes(xlsx_inline(), ".xlsx")
    assert xlsx.sheet_names == ("BOM",)
    assert xlsx.header_candidates["BOM"][0] == ("part_number", "description", "quantity")


def test_html_xls_profile_accepts_arena_engineering_entities():
    profile = profile_bytes(arena_html("&plusmn;25PPM/&deg;C &amp; &Acirc;&plusmn; tolerance"), ".xls")
    assert profile.source_subtype == "html_table_xls"
    assert profile.sheet_names == ("HTMLTable",)
    assert profile.header_candidates["HTMLTable"][0] == ("item_number", "item_name", "quantity")


def test_html_xls_profile_rejects_unapproved_named_entities():
    with pytest.raises(RegistryError, match="PARSER_FORMAT_NOT_RECOGNIZED"):
        profile_bytes(arena_html("&copy;"), ".xls")


def test_xlsx_rejects_unsafe_relationship_and_zip_bounds():
    data = xlsx_inline()
    input_zip = zipfile.ZipFile(io.BytesIO(data))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for info in input_zip.infolist():
            value = input_zip.read(info.filename)
            if info.filename.endswith("workbook.xml.rels"):
                value = value.replace(b"worksheets/sheet1.xml", b"../../outside.xml")
            archive.writestr(info.filename, value)
    with pytest.raises(RegistryError, match="PARSER_FORMAT_NOT_RECOGNIZED"):
        profile_bytes(output.getvalue(), ".xlsx")


def _with_extra_relationships(extra: str, extra_parts=()) -> bytes:
    """Synthetic xlsx whose workbook rels carry extra relationships (Office/SharePoint shape)."""
    data = xlsx_inline()
    source = zipfile.ZipFile(io.BytesIO(data))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for info in source.infolist():
            value = source.read(info.filename)
            if info.filename.endswith("workbook.xml.rels"):
                value = value.replace(b"</Relationships>", extra.encode() + b"</Relationships>")
            archive.writestr(info.filename, value)
        for name, value in extra_parts:
            archive.writestr(name, value)
    return output.getvalue()


def test_xlsx_accepts_internal_custom_xml_relationships_outside_xl():
    # Regression (QQ-26-A030): Office/SharePoint workbooks link customXml parts at the
    # package root via "../customXml/itemN.xml". These are internal and must not refuse the file.
    extra = "".join(f'<Relationship Id="rId{n}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/customXml" Target="../customXml/item{n}.xml"/>' for n in (2, 3, 4))
    parts = [(f"customXml/item{n}.xml", b'<?xml version="1.0"?><root/>') for n in (2, 3, 4)]
    profile = profile_bytes(_with_extra_relationships(extra, parts), ".xlsx")
    assert profile.sheet_names == ("BOM",)
    assert profile.header_candidates["BOM"][0] == ("part_number", "description", "quantity")
    assert profile.profile_hash == profile_bytes(xlsx_inline(), ".xlsx").profile_hash
    absolute = '<Relationship Id="rId9" Type="customXml" Target="/customXml/item9.xml"/>'
    assert profile_bytes(_with_extra_relationships(absolute), ".xlsx").sheet_names == ("BOM",)


@pytest.mark.parametrize("relationship", [
    '<Relationship Id="rId9" Type="customXml" Target="../../outside.xml"/>',
    '<Relationship Id="rId9" Type="customXml" Target="/../outside.xml"/>',
    '<Relationship Id="rId9" Type="customXml" Target=".."/>',
    '<Relationship Id="rId9" Type="customXml" Target=""/>',
    '<Relationship Id="rId9" Type="customXml" Target="..\\customXml\\item1.xml"/>',
    '<Relationship Id="rId9" Type="customXml" Target="file:///C:/x.xml"/>',
    '<Relationship Id="rId9" Type="customXml" Target="C:/x.xml"/>',
    '<Relationship Id="rId9" Type="hyperlink" Target="https://example.invalid/x" TargetMode="External"/>',
])
def test_xlsx_still_refuses_external_escaping_and_malformed_relationships(relationship):
    with pytest.raises(RegistryError, match="PARSER_FORMAT_NOT_RECOGNIZED") as error:
        profile_bytes(_with_extra_relationships(relationship), ".xlsx")
    assert error.value.details == {"reason": "unsafe_relationship"}


def test_xlsx_never_reads_a_sheet_outside_xl():
    data = xlsx_inline()
    source = zipfile.ZipFile(io.BytesIO(data))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for info in source.infolist():
            value = source.read(info.filename)
            name = info.filename
            if name.endswith("workbook.xml.rels"):
                value = value.replace(b"worksheets/sheet1.xml", b"../other/sheet1.xml")
            if name == "xl/worksheets/sheet1.xml":
                name = "other/sheet1.xml"
            archive.writestr(name, value)
    profile = profile_bytes(output.getvalue(), ".xlsx")
    assert profile.sheet_names == ("Sheet1",)
    assert profile.header_candidates == {"Sheet1": ()}


def test_selection_is_parser_scoped_exact_one_no_match_and_ambiguous(tmp_path):
    sig = signature()
    code, _, candidate, approval = records()
    store = LocalRegistryStore(tmp_path)
    store.register_candidate(candidate, code)
    variant = store.promote(candidate, approval, sig, code)
    registry = MasterRegistry([variant], {sig.signature_hash: sig})
    assert registry.select("Astronics", csv_bytes(), ".csv") == variant
    with pytest.raises(RegistryError) as missing:
        registry.select("Other", csv_bytes(), ".csv")
    assert missing.value.code == "PARSER_FORMAT_NOT_RECOGNIZED"
    registry.register_variant(type(variant)("Astronics", "format2", "1", "approved/Astronics/format2/1/parse.py", variant.code_sha256, variant.signature_hash, variant.rule_hash, "approval2"), sig)
    with pytest.raises(RegistryError) as ambiguous:
        registry.select("Astronics", csv_bytes(), ".csv")
    assert ambiguous.value.code == "PARSER_FORMAT_AMBIGUOUS"


def test_candidate_path_is_quarantined_and_code_never_executes(tmp_path):
    code, sig, candidate, approval = records()
    marker = tmp_path / "executed"
    malicious = f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n".encode()
    candidate = CandidateRecord(candidate.candidate_id, candidate.parser_key, candidate.customer, candidate.code_path, sha(malicious), candidate.signature_hash, candidate.rule_hash, candidate.fixture_bindings, "passed", candidate.created_identity, candidate.created_time)
    approval = ApprovalRecord(approval.approval_id, candidate.candidate_id, candidate.parser_key, candidate.customer, approval.variant_id, approval.version, candidate.candidate_hash, candidate.code_sha256, sig.signature_hash, sig.rule_hash, candidate.fixture_hash, approval.approver, approval.approved_time)
    store = LocalRegistryStore(tmp_path / "registry")
    store.register_candidate(candidate, malicious)
    store.promote(candidate, approval, sig, malicious)
    assert not marker.exists()
    bad = CandidateRecord("candidate2", "Astronics", "customer1", "candidates/../approved/parse.py", sha(code), sig.signature_hash, sig.rule_hash, candidate.fixture_bindings, "passed", "author1", candidate.created_time)
    with pytest.raises(RegistryError, match="CANDIDATE_PATH_QUARANTINED"):
        store.register_candidate(bad, code)


def test_approval_binds_code_signature_fixtures_and_parser(tmp_path):
    code, sig, candidate, approval = records()
    store = LocalRegistryStore(tmp_path)
    store.register_candidate(candidate, code)
    changed = ApprovalRecord(approval.approval_id, approval.candidate_id, "Other", approval.customer, approval.variant_id, approval.version, approval.candidate_hash, approval.code_sha256, approval.signature_hash, approval.rule_hash, approval.fixture_hash, approval.approver, approval.approved_time)
    with pytest.raises(RegistryError, match="REGISTRY_CONFLICT"):
        store.promote(candidate, changed, sig, code)
    with pytest.raises(RegistryError, match="REGISTRY_CONFLICT"):
        store.promote(candidate, approval, sig, code + b"# changed")
    other_signature = VariantSignature(frozenset({".csv"}), required_headers={"Sheet1": frozenset({"different"})})
    changed_signature_approval = ApprovalRecord("approval3", candidate.candidate_id, candidate.parser_key, candidate.customer, approval.variant_id, approval.version, candidate.candidate_hash, candidate.code_sha256, other_signature.signature_hash, other_signature.rule_hash, candidate.fixture_hash, approval.approver, approval.approved_time)
    with pytest.raises(RegistryError, match="REGISTRY_CONFLICT"):
        store.promote(candidate, changed_signature_approval, other_signature, code)


def test_unregistered_candidate_cannot_be_promoted(tmp_path):
    code, sig, candidate, approval = records()
    with pytest.raises(RegistryError, match="REGISTRY_CONFLICT"):
        LocalRegistryStore(tmp_path).promote(candidate, approval, sig, code)


def test_promotion_replay_reload_and_conflict_preserve_prior_variant(tmp_path):
    code, sig, candidate, approval = records()
    store = LocalRegistryStore(tmp_path)
    store.register_candidate(candidate, code)
    first = store.promote(candidate, approval, sig, code)
    assert first.code_path == "approved/Astronics/format1/1/parse.py"
    assert store.promote(candidate, approval, sig, code) == first
    reloaded = LocalRegistryStore(tmp_path)
    assert reloaded.version == 1
    assert reloaded.registry().select("Astronics", csv_bytes(), ".csv") == first
    conflicting = ApprovalRecord("approval2", candidate.candidate_id, candidate.parser_key, candidate.customer, approval.variant_id, approval.version, candidate.candidate_hash, candidate.code_sha256, sig.signature_hash, sig.rule_hash, candidate.fixture_hash, "admin2", approval.approved_time)
    with pytest.raises(RegistryError, match="REGISTRY_CONFLICT"):
        reloaded.promote(candidate, conflicting, sig, code)
    assert reloaded.registry().variants == [first]
