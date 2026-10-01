"""Read and summarize immutable candidate validation output for human review."""
from __future__ import annotations

import hashlib
import io
import json
import re
from pathlib import PurePosixPath
from typing import Any

from .core import OUTPUT_CONTENT_TYPE
from .remote import _validate_xlsx_bytes
from .arena_policy import verify_evidence
from .semantic import AssemblyIdentity, compare_boms, normalize_bom
from .semantic_facts import SemanticFactsError, facts_from_rows

MAX_METADATA_BYTES = 1_000_000
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_MPN_HEADERS = {
    "mpn", "manufacturerpartnumber", "manufacturerpn", "mfrpartnumber",
    "mfrpn", "mfgpartnumber", "mfgpn",
}
_IDENTITY_HEADERS = _MPN_HEADERS | {
    "partnumber", "partno", "pn", "itemnumber", "itemno", "description",
    "itemname", "quantity", "qty", "referencedesignator", "refdes",
}


class CandidateReviewError(ValueError):
    """Safe failure while binding review evidence."""


def _json(storage: Any, path: str, *, candidate: bool = False) -> dict[str, Any]:
    data = (storage.download_candidate if candidate else storage.download_registry)(path, MAX_METADATA_BYTES)
    try:
        value = json.loads(data.decode("utf-8"))
    except Exception:
        raise CandidateReviewError("REVIEW_EVIDENCE_INVALID") from None
    if not isinstance(value, dict):
        raise CandidateReviewError("REVIEW_EVIDENCE_INVALID")
    return value


def _norm(value: Any) -> str:
    if value is None:
        return ""
    try:
        import pandas as pd
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return re.sub(r"[^a-z0-9]+", "", str(value).strip().lower())


def _source_frames(data: bytes, suffix: str) -> dict[str, Any]:
    import pandas as pd
    stream = io.BytesIO(data)
    if suffix == ".csv":
        return {"CSV": pd.read_csv(stream, header=None, dtype=object, keep_default_na=False)}
    if suffix in {".xls", ".xlsx"}:
        engine = "xlrd" if suffix == ".xls" else "openpyxl"
        return pd.read_excel(stream, sheet_name=None, header=None, dtype=object, engine=engine)
    return {}


def _source_counts(data: bytes, suffix: str) -> dict[str, int | None]:
    """Use only an unambiguous header; unknown is safer than a guessed count."""
    try:
        frames = _source_frames(data, suffix)
    except Exception:
        return {"line_items": None, "mpn_cells": None, "distinct_mpns": None}
    candidates: list[tuple[int, str, int, Any, list[str]]] = []
    for sheet_name, frame in frames.items():
        if len(frame) > 100_000 or len(frame.columns) > 512:
            continue
        for row_index in range(min(len(frame), 100)):
            headers = [_norm(value) for value in frame.iloc[row_index].tolist()]
            score = len(set(headers) & _IDENTITY_HEADERS)
            if score >= 2 and any(value in _IDENTITY_HEADERS for value in headers):
                candidates.append((score, str(sheet_name), row_index, frame, headers))
    if not candidates:
        return {"line_items": None, "mpn_cells": None, "distinct_mpns": None}
    best_score = max(item[0] for item in candidates)
    best = [item for item in candidates if item[0] == best_score]
    if len(best) != 1:
        return {"line_items": None, "mpn_cells": None, "distinct_mpns": None}
    _, _, row_index, frame, headers = best[0]
    rows = frame.iloc[row_index + 1:]
    populated = rows.apply(lambda row: any(_norm(value) for value in row.tolist()), axis=1)
    rows = rows[populated]
    mpn_indexes = [index for index, header in enumerate(headers) if header in _MPN_HEADERS]
    if not mpn_indexes:
        return {"line_items": int(len(rows)), "mpn_cells": None, "distinct_mpns": None}
    values = [_norm(value) for index in mpn_indexes for value in rows.iloc[:, index].tolist()]
    values = [value for value in values if value]
    return {"line_items": int(len(rows)), "mpn_cells": len(values), "distinct_mpns": len(set(values))}


def _parsed_counts(output: bytes) -> dict[str, int]:
    import pandas as pd
    frame = pd.read_excel(io.BytesIO(output), engine="openpyxl")
    columns = [name for name in frame.columns if re.fullmatch(r"manufacturer_item_number_[1-8]", str(name))]
    values = [_norm(value) for name in columns for value in frame[name].tolist()]
    values = [value for value in values if value]
    return {"line_items": int(len(frame)), "mpn_cells": len(values), "distinct_mpns": len(set(values))}


def _parsed_rows(output: bytes) -> list[dict[str, Any]]:
    import pandas as pd
    return pd.read_excel(io.BytesIO(output), engine="openpyxl").to_dict(orient="records")


def _manifest_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class CandidateReviewService:
    def __init__(self, storage: Any, *, max_source_bytes: int = 50_000_000,
                 max_output_bytes: int = 50_000_000, max_rows: int = 100_000):
        self.storage = storage
        self.max_source_bytes = max_source_bytes
        self.max_output_bytes = max_output_bytes
        self.max_rows = max_rows

    def get(self, request_id: str, candidate_id: str, validation_id: str) -> tuple[dict[str, Any], bytes]:
        if not all(isinstance(value, str) and _ID.fullmatch(value)
                   for value in (request_id, candidate_id, validation_id)):
            raise CandidateReviewError("INVALID_REVIEW_ID")
        request = _json(self.storage, f"candidates/requests/{request_id}.json")
        evidence = _json(self.storage, (f"candidates/{request_id}/{candidate_id}/validation/"
                                        f"{validation_id}/evidence.json"), candidate=True)
        if (request.get("request_id") != request_id or evidence.get("request_id") != request_id
                or evidence.get("candidate_id") != candidate_id
                or evidence.get("validation_id") != validation_id
                or evidence.get("status") != "awaiting_approval"
                or evidence.get("schema", {}).get("valid") is not True):
            raise CandidateReviewError("REVIEW_BINDING_INVALID")
        sources = request.get("source_evidence")
        fixtures = evidence.get("fixtures")
        # The current validator produces one fixture. Never imply that a
        # multi-source request was fully reviewed.
        if not isinstance(sources, list) or len(sources) != 1 or not isinstance(fixtures, list) or len(fixtures) != 1:
            raise CandidateReviewError("REVIEW_INCOMPLETE")
        source = sources[0]
        fixture = fixtures[0]
        if (not isinstance(source, dict) or not isinstance(fixture, dict)
                or fixture.get("document_id") != source.get("document_id")
                or fixture.get("sha256") != source.get("sha256")
                or fixture.get("size_bytes") != source.get("size_bytes")):
            raise CandidateReviewError("REVIEW_BINDING_INVALID")
        output_meta = evidence.get("output")
        if not isinstance(output_meta, dict):
            raise CandidateReviewError("REVIEW_EVIDENCE_INVALID")
        output_path = output_meta.get("blob_path")
        if (not isinstance(output_path, str)
                or not output_path.startswith(f"candidates/{request_id}/{candidate_id}/validation/{validation_id}/")
                or output_meta.get("content_type") != OUTPUT_CONTENT_TYPE
                or not isinstance(output_meta.get("size_bytes"), int)
                or not 0 < output_meta["size_bytes"] <= self.max_output_bytes
                or not isinstance(output_meta.get("sha256"), str)
                or not _HASH.fullmatch(output_meta["sha256"])):
            raise CandidateReviewError("REVIEW_EVIDENCE_INVALID")
        output = self.storage.download_candidate(output_path, self.max_output_bytes)
        if len(output) != output_meta["size_bytes"] or hashlib.sha256(output).hexdigest() != output_meta["sha256"]:
            raise CandidateReviewError("REVIEW_OUTPUT_INVALID")
        try:
            _validate_xlsx_bytes(output, self.max_rows)
        except Exception:
            raise CandidateReviewError("REVIEW_OUTPUT_INVALID") from None
        source_path = source.get("blob_path")
        if (not isinstance(source_path, str) or not isinstance(source.get("size_bytes"), int)
                or not isinstance(source.get("sha256"), str) or not _HASH.fullmatch(source["sha256"])):
            raise CandidateReviewError("REVIEW_EVIDENCE_INVALID")
        source_bytes = self.storage.download_source(source_path, self.max_source_bytes)
        if len(source_bytes) != source["size_bytes"] or hashlib.sha256(source_bytes).hexdigest() != source["sha256"]:
            raise CandidateReviewError("REVIEW_SOURCE_INVALID")
        summary = {
            "version": "candidate-review.v1",
            "source": {"document_id": source.get("document_id"), "blob_path": source_path,
                       "sha256": source["sha256"],
                       "size_bytes": source["size_bytes"],
                       **_source_counts(source_bytes, PurePosixPath(source_path).suffix.lower())},
            "parsed": {"blob_path": output_path, "sha256": output_meta["sha256"], "size_bytes": output_meta["size_bytes"],
                       **_parsed_counts(output)},
        }
        if isinstance(evidence.get("arena_policy"), dict):
            policy = evidence["arena_policy"]
            if not verify_evidence(policy):
                raise CandidateReviewError("REVIEW_POLICY_INVALID")
            summary["version"] = "candidate-review.v2"
            summary["arena_policy"] = {
                key: policy[key] for key in ("version", "valid", "reason", "source_rows", "output_rows",
                                            "included_count", "omitted_count", "omissions", "decision_hash")
                if key in policy
            }
        return summary, output

    def get_v3(self, request_id: str, candidate_id: str, validation_id: str) -> dict[str, Any]:
        """Return a byte-bound semantic review manifest for every unique fixture."""
        if not all(isinstance(value, str) and _ID.fullmatch(value)
                   for value in (request_id, candidate_id, validation_id)):
            raise CandidateReviewError("INVALID_REVIEW_ID")
        path = f"candidates/{request_id}/{candidate_id}/validation/{validation_id}/evidence.json"
        evidence = _json(self.storage, path, candidate=True)
        request = _json(self.storage, f"candidates/requests/{request_id}.json")
        if (request.get("request_id") != request_id or evidence.get("request_id") != request_id
                or evidence.get("candidate_id") != candidate_id
                or evidence.get("validation_id") != validation_id):
            raise CandidateReviewError("REVIEW_BINDING_INVALID")
        sources = request.get("source_evidence")
        fixtures = evidence.get("fixtures")
        if not isinstance(sources, list) or not isinstance(fixtures, list) or not fixtures:
            raise CandidateReviewError("REVIEW_INCOMPLETE")
        # Normalize the pre-group singleton evidence shape without projecting a
        # grouped request into one fixture.
        if len(sources) == 1 and len(fixtures) == 1 and "fixture_sha256" not in fixtures[0]:
            legacy = dict(fixtures[0])
            legacy["fixture_sha256"] = legacy.get("sha256")
            legacy["source_aliases"] = [{"document_id": sources[0].get("document_id"),
                                          "blob_path": sources[0].get("blob_path")}]
            legacy.setdefault("output", evidence.get("output"))
            legacy.setdefault("status", "passed")
            legacy.setdefault("semantic", evidence.get("semantic", {}))
            fixtures = [legacy]
        expected: dict[str, list[dict[str, Any]]] = {}
        for source in sources:
            if (not isinstance(source, dict) or not isinstance(source.get("sha256"), str)
                    or not _HASH.fullmatch(source["sha256"])):
                raise CandidateReviewError("REVIEW_BINDING_INVALID")
            expected.setdefault(str(source.get("sha256", "")).lower(), []).append(source)
        actual: dict[str, dict[str, Any]] = {}
        for item in fixtures:
            if (not isinstance(item, dict) or not isinstance(item.get("fixture_sha256"), str)
                    or not _HASH.fullmatch(item["fixture_sha256"])):
                raise CandidateReviewError("REVIEW_BINDING_INVALID")
            if item["fixture_sha256"].lower() in actual:
                raise CandidateReviewError("REVIEW_BINDING_INVALID")
            actual[item["fixture_sha256"].lower()] = item
        if set(expected) != set(actual) or any(
            sorted((a.get("document_id"), a.get("blob_path")) for a in expected[digest]) !=
            sorted((a.get("document_id"), a.get("blob_path")) for a in actual[digest].get("source_aliases", []))
            for digest in expected
        ):
            raise CandidateReviewError("REVIEW_INCOMPLETE")
        ordered = [actual[digest] for digest in sorted(actual)]
        complete = (evidence.get("status") == "awaiting_approval" and len(ordered) == len(expected)
                    and evidence.get("complete", True) is True
                    and evidence.get("fixture_count", len(ordered)) == len(ordered)
                    and all(item.get("status") == "passed" and isinstance(item.get("output"), dict)
                            for item in ordered))
        reviewed: list[dict[str, Any]] = []
        rows_by_hash: dict[str, list[dict[str, Any]]] = {}
        for item in ordered:
            digest = item["fixture_sha256"].lower()
            output = item.get("output")
            if not isinstance(output, dict):
                raise CandidateReviewError("REVIEW_EVIDENCE_INVALID")
            output_path = output.get("blob_path")
            if (not isinstance(output_path, str) or not output_path.startswith(
                    f"candidates/{request_id}/{candidate_id}/validation/{validation_id}/")
                    or output.get("content_type") != OUTPUT_CONTENT_TYPE
                    or type(output.get("size_bytes")) is not int
                    or not 0 < output["size_bytes"] <= self.max_output_bytes
                    or not isinstance(output.get("sha256"), str)
                    or not _HASH.fullmatch(output["sha256"])):
                raise CandidateReviewError("REVIEW_EVIDENCE_INVALID")
            data = self.storage.download_candidate(output_path, self.max_output_bytes)
            if len(data) != output["size_bytes"] or hashlib.sha256(data).hexdigest() != output["sha256"]:
                raise CandidateReviewError("REVIEW_OUTPUT_INVALID")
            try:
                _validate_xlsx_bytes(data, self.max_rows)
                rows_by_hash[digest] = _parsed_rows(data)
                facts = facts_from_rows(rows_by_hash[digest], output_sha256=output["sha256"])
            except (Exception, SemanticFactsError):
                raise CandidateReviewError("REVIEW_OUTPUT_INVALID") from None
            semantic = item.get("semantic")
            claimed_identity = semantic.get("assembly_identity") if isinstance(semantic, dict) else None
            identity = None
            if facts.assembly is not None:
                name, revision = facts.assembly.normalized()
                identity = {"name": name, "revision": revision}
            if (not isinstance(semantic, dict)
                    or semantic.get("fingerprint") != facts.fingerprint
                    or semantic.get("model_summary") != facts.model_summary
                    or claimed_identity != identity):
                raise CandidateReviewError("REVIEW_SEMANTIC_BINDING")
            mapping_required = identity is None
            reviewed.append({
                "fixture_sha256": digest,
                "source_aliases": sorted(item.get("source_aliases", []),
                                         key=lambda alias: (str(alias.get("document_id", "")), str(alias.get("blob_path", "")))),
                "output": {key: output[key] for key in ("blob_path", "sha256", "size_bytes", "content_type")},
                "assembly_identity": identity if not mapping_required else None,
                "mapping_required": mapping_required,
                "fingerprint": semantic.get("fingerprint") if isinstance(semantic, dict) else None,
                "model_summary": semantic.get("model_summary", {}) if isinstance(semantic, dict) else {},
            })

        grouped: dict[tuple[str, str] | None, list[dict[str, Any]]] = {}
        for item in reviewed:
            identity = item["assembly_identity"]
            key = None if identity is None else AssemblyIdentity(
                str(identity.get("name", "")), str(identity.get("revision", ""))).normalized()
            grouped.setdefault(key, []).append(item)
        groups = []
        for identity_key in sorted(grouped, key=lambda value: value or ("", "")):
            members = sorted(grouped[identity_key], key=lambda item: item["fixture_sha256"])
            comparisons, conflicts, wins = [], False, {item["fixture_sha256"]: 0 for item in members}
            assembly = AssemblyIdentity(*(identity_key or ("", "")))
            for left_index, left in enumerate(members):
                for right in members[left_index + 1:]:
                    comparison = compare_boms(
                        rows_by_hash[left["fixture_sha256"]], rows_by_hash[right["fixture_sha256"]],
                        left_assembly=assembly, right_assembly=assembly)
                    record = {"left_fixture_sha256": left["fixture_sha256"],
                              "right_fixture_sha256": right["fixture_sha256"],
                              "relation": comparison.relation,
                              "material_differences": list(comparison.evidence)}
                    if comparison.relation == "dominates":
                        side = next((entry.get("side") for entry in comparison.evidence
                                     if entry.get("kind") == "optional_enrichment"), None)
                        selected = right["fixture_sha256"] if side == "right" else left["fixture_sha256"]
                        record["selected_fixture_sha256"] = selected
                        wins[selected] += 1
                    elif comparison.relation == "equivalent":
                        record["equivalent_aliases"] = [left["fixture_sha256"], right["fixture_sha256"]]
                    elif comparison.relation == "compatible":
                        record["compatible_fixtures"] = [left["fixture_sha256"], right["fixture_sha256"]]
                    else:
                        conflicts = True
                    comparisons.append(record)
            selected = None
            if identity_key is not None and not conflicts:
                selected = max(members, key=lambda item: (wins[item["fixture_sha256"]], item["fixture_sha256"]))["fixture_sha256"]
            groups.append({"group_id": _manifest_hash({"assembly_identity": identity_key}),
                           "assembly_identity": None if identity_key is None else {"name": identity_key[0], "revision": identity_key[1]},
                           "mapping_required": identity_key is None,
                           "fixtures": members,
                           "source_document_ids": sorted({str(alias.get("document_id")) for item in members for alias in item["source_aliases"]}),
                           "comparisons": comparisons, "has_conflicts": conflicts,
                           "selected_fixture_sha256": selected})
        approval_ready = complete and all(not group["mapping_required"] and not group["has_conflicts"] for group in groups)
        manifest = {"version": "candidate-review.v3", "request_id": request_id, "candidate_id": candidate_id,
                    "validation_id": validation_id, "complete": complete, "validation_complete": complete,
                    "approval_ready": approval_ready, "status": evidence.get("status"),
                    "source_count": len(sources), "unique_fixture_count": len(ordered), "groups": groups,
                    "candidate": evidence.get("candidate"), "policy_versions": evidence.get("policy_versions", [])}
        return {**manifest, "evidence": {"candidate": evidence.get("candidate"),
                "policy_versions": evidence.get("policy_versions", []), "manifest_sha256": _manifest_hash(manifest)}}
