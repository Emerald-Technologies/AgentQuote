"""Synthetic v6 stage 4 packages and a scripted model that answers from the packet it is shown.

No customer data, no services. Used by tests/test_package_evidence_compact.py.
"""
import io

from openpyxl import Workbook

from bom_processor.core import CANONICAL_COLUMNS
from bom_processor.package_evidence_contracts import digest
from bom_processor.package_evidence_proposal import produce_proposal

V6 = "package-evidence-foundry.v6"
QQ = "12345678-1234-4234-8234-123456789012"


def uuid(kind: int, index: int) -> str:
    return f"12345678-1234-4234-8234-{kind:04d}{index:08d}"


def bom(lines, *, pcb=None, pairs=2):
    """Canonical workbook: optional PCB line first, then commercial lines."""
    wb = Workbook()
    ws = wb.active
    ws.title = "BOM"
    ws.append(CANONICAL_COLUMNS)
    number = 0
    if pcb is not None:
        number += 1
        ws.append([number, pcb, "PCB, bare board", "A", "Part", "EA", 1, None, None]
                  + [None] * (len(CANONICAL_COLUMNS) - 9))
    for i in range(1, lines + 1):
        number += 1
        row = [number, f"RES-{i:05d}", f"RES 10K 1% 0603 {i}", None, "Part", "EA", 2, f"R{i}", None]
        for p in range(1, 9):
            row += ["VISHAY", f"CRCW{i:05d}{p}"] if p <= pairs else [None, None]
        ws.append(row)
    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()


def text_root(data: bytes, name: str, index: int) -> dict:
    return {"documentId": uuid(3, index), "sha256": digest(data), "sizeBytes": len(data), "fileName": name, "data": data}


def gerber(part: str) -> bytes:
    coordinates = "".join(f"X{i * 100}Y{i * 50}D01*\n" for i in range(4000))
    return (f"G04 Board {part} Rev A top copper*\n%FSLAX25Y25*%\n%MOIN*%\n%ADD10C,0.010*%\nD10*\n"
            f"{coordinates}M02*\n").encode()


def package(assemblies, *, shared_pcb=None, lines=20, extra_files=()):
    """assemblies: list of (part number, pcb part or None). Returns (context, roots, workbooks)."""
    context_assemblies, books, roots = [], {}, []
    index = 0
    for n, (part, pcb) in enumerate(assemblies, 1):
        data = bom(lines, pcb=pcb or shared_pcb)
        document = uuid(2, n)
        books[document] = data
        context_assemblies.append({"assemblyUuid": uuid(1, n), "partNumber": part, "revision": "A",
                                   "canonicalBom": {"documentId": document, "sha256": digest(data),
                                                    "sizeBytes": len(data)}})
        index += 1
        roots.append(text_root(f"ASSEMBLY DRAWING\nPart Number: {part}\nRevision: A\n".encode(),
                               f"{part}_assembly.txt", index))
        if pcb:
            index += 1
            roots.append(text_root(gerber(pcb), f"{pcb}_top.gbr", index))
    if shared_pcb:
        index += 1
        roots.append(text_root(gerber(shared_pcb), f"common/{shared_pcb}_top.gbr".replace("/", "_"), index))
    for name, data in extra_files:
        index += 1
        roots.append(text_root(data, name, index))
    # The customer's original BOM upload (code-retained provenance, never judged by the model).
    index += 1
    roots.append(text_root(b"original customer BOM workbook bytes", "original.xlsx", index))
    context = {"qqUuid": QQ, "packageVersion": 1, "bomPlanId": "a" * 64, "bomApprovalId": "b" * 64,
               "resolvedManifestSha256": "c" * 64, "assemblies": context_assemblies}
    return context, roots, books


class ScriptedModel:
    """Answers like a careful agent from what it is shown: a file whose path or header names a part
    number on a shown line is linked to those lines; an assembly file is linked to its assembly."""

    def __init__(self, *, fail_assembly=None, bad_first=False, review_fail_assembly=None):
        self.packets, self.reviews = [], []
        self.fail_assembly, self.bad_first, self.review_fail_assembly = fail_assembly, bad_first, review_fail_assembly

    def propose(self, packet):
        self.packets.append(packet)
        names = {a["partNumber"] for a in packet["assemblies"]}
        if self.fail_assembly in names:
            raise RuntimeError("synthetic failure")
        if self.bad_first and "previousReplyRejected" not in packet:
            return {"files": [], "parts": [], "requirements": []}
        asm = {a["id"]: a for a in packet["assemblies"]}
        part_lines = [line for line in packet["lines"] if line["cat"] == "Part"]
        files = []
        for view in packet["files"]:
            shown = " ".join([view["path"], *view.get("header", []), view.get("text", ""), view.get("head", "")])
            pcb_lines = [l for l in part_lines if l["pn"].startswith("PCB-") and l["pn"] in shown]
            owner = [a for a in asm.values() if f"Part Number: {a['partNumber']}" in shown]
            links = []
            if pcb_lines and view.get("quotable"):
                cite = next(line for line in (view.get("header") or view.get("text", "").split("\n")) if pcb_lines[0]["pn"] in line)
                assemblies = sorted({l["asm"] for l in pcb_lines})
                links.append({"lines": [l["id"] for l in pcb_lines], "assemblies": assemblies, "role": "artwork",
                              "sharing": "unknown" if len(assemblies) > 1 else "not_shared",
                              "rationale": "File name and header name the PCB part number.",
                              "cite": [{"file": view["id"], "text": cite}]})
            elif owner and view.get("quotable"):
                links.append({"lines": [], "assemblies": [owner[0]["id"]], "role": "assembly_drawing", "sharing": "not_shared",
                              "rationale": "The drawing names this assembly.",
                              "cite": [{"file": view["id"], "text": f"Part Number: {owner[0]['partNumber']}"}]})
            files.append({"file": view["id"], "state": "included" if links else "unrelated", "replacement": None,
                          "rationale": "Linked by name and content." if links else "Not for these assemblies.",
                          "links": links})
        drawing = next((f for f in files if f["links"] and f["links"][0]["role"] == "assembly_drawing"), None)
        cite = drawing["links"][0]["cite"] if drawing else [files[0]["links"][0]["cite"][0]] if files and files[0]["links"] else []
        pcb = [l["id"] for l in part_lines if l["pn"].startswith("PCB-")]
        other = [l["id"] for l in part_lines if not l["pn"].startswith("PCB-")]
        parts = ([{"lines": pcb, "partType": "pcb", "sourceRequiresDrawing": True, "sourceRequiresRevision": True,
                   "rationale": "Bare board.", "conflicts": []}] if pcb else []) + (
                 [{"lines": other, "partType": "commercial", "sourceRequiresDrawing": False,
                   "sourceRequiresRevision": False, "rationale": "Catalog resistors with MPNs.", "conflicts": []}]
                 if other else [])
        subjects = [a for a in asm if asm[a]["requirement"]] + [l["id"] for l in part_lines]
        requirements = [{"subjects": subjects, "requiredRoles": [], "determination": "unknown",
                         "rationale": "Offline requirements.", "cite": cite, "conflicts": []}] if subjects and cite else []
        return {"files": files, "parts": parts, "requirements": requirements}

    def review(self, packet):
        self.reviews.append(packet)
        names = {a["partNumber"] for a in packet["assemblies"]}
        if self.review_fail_assembly in names:
            raise RuntimeError("synthetic review failure")
        return {"claims": [{"claim": c["id"], "verdict": "agree", "reason": "ok", "explanation": "Consistent.",
                            "quotes": []} for c in packet["claims"]], "findings": [], "verdict": "proceed"}


def source_proof(roots):
    source = next(r for r in roots if r["fileName"] == "original.xlsx")
    return {"requestFingerprint": "e" * 64, "stagingEventId": "12345678-1234-4234-8234-123456789080",
            "sources": [{k: source[k] for k in ("documentId", "sha256", "sizeBytes")}]}


def produce(args, model=None, **kwargs):
    context, roots, books = args
    return produce_proposal(context, roots, books, model=model or ScriptedModel(), producer="offline-test",
                            model_name="fake-model", policy_version=V6, prompt_sha256="d" * 64, run_id="run1",
                            include_classification=True, include_manufacturing=True,
                            retained_bom_sources=source_proof(roots), **kwargs)
