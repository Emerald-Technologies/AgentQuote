"""Select current documentary policy proof without discarding passed history.

Input: immutable validation records. Output: narrowly eligible superseded failure.
Run tests/test_document_roles.py; no storage writes or model calls.
"""
def superseded_layout_failure(evidence):
    roles = evidence.get("semantic", {}).get("document_roles", {})
    return (evidence.get("version") == "candidate-validation.v4"
        and all(isinstance(evidence.get(key), str) and evidence[key] for key in
            ("request_id", "candidate_id", "validation_id"))
        and isinstance(evidence.get("candidate"), dict)
        and isinstance(evidence["candidate"].get("sha256"), str)
        and isinstance(evidence.get("fixtures"), list) and len(evidence["fixtures"]) == 1
        and isinstance(evidence["fixtures"][0], dict)
        and isinstance(evidence["fixtures"][0].get("sha256"), str)
        and evidence.get("document_role_policy") == "xlsx-document-roles.v1"
            and evidence.get("status") == "generated_unvalidated"
            and evidence.get("schema", {}).get("valid") is True
            and evidence.get("semantic", {}).get("valid") is False
            and roles.get("version") == "xlsx-document-roles.v1"
            and roles.get("valid") is False and roles.get("result") == "source_roles_ambiguous")