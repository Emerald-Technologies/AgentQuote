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
