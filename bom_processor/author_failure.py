"""Safe author diagnostics: stage/category only, never exception text or content.

Inputs: exceptions at author boundaries. Outputs: allowlisted diagnostic fields.
Run: python -m pytest tests/test_author_failure.py.
"""
from contextlib import contextmanager
import json

STAGES = frozenset({"inventory", "invocation", "response", "association", "code_validation"})
CATEGORIES = frozenset({"json_invalid", "timeout", "connection", "http_error", "value_error", "type_error", "unexpected"})
# json.JSONDecodeError.msg is a fixed stdlib phrase (positions are separate attributes),
# so mapping its prefix to an allowlisted code records the malformation kind, never content.
_JSON_REASONS = (
    ("Expecting value", "expecting_value"),
    ("Invalid control character", "invalid_control_character"),
    ("Invalid \\escape", "invalid_escape"),
    ("Invalid \\uXXXX escape", "invalid_escape"),
    ("Extra data", "extra_data"),
    ("Unterminated string", "unterminated_string"),
    ("Expecting ',' delimiter", "expecting_delimiter"),
    ("Expecting ':' delimiter", "expecting_delimiter"),
    ("Expecting property name", "expecting_property_name"),
)
JSON_REASONS = frozenset(code for _, code in _JSON_REASONS) | {"other"}


def json_reason(exception):
    message = getattr(exception, "msg", None)
    if not isinstance(message, str):
        return "other"
    return next((code for prefix, code in _JSON_REASONS if message.startswith(prefix)), "other")


def diagnostic_fields(stage, category, http_status=None, json_reason=None):
    if stage not in STAGES or category not in CATEGORIES:
        return {}
    fields = {"failure_stage": stage, "failure_category": category}
    if type(http_status) is int and 100 <= http_status <= 599:
        fields["failure_http_status"] = http_status
    if category == "json_invalid" and json_reason in JSON_REASONS:
        fields["failure_json_reason"] = json_reason
    return fields


class AuthorFailure(ValueError):
    def __init__(self, stage, exception):
        name = type(exception).__name__
        if isinstance(exception, json.JSONDecodeError):
            category = "json_invalid"
        elif isinstance(exception, TimeoutError) or name == "APITimeoutError":
            category = "timeout"
        elif isinstance(exception, ConnectionError) or name == "APIConnectionError":
            category = "connection"
        elif name in {"APIStatusError", "BadRequestError", "RateLimitError", "AuthenticationError", "PermissionDeniedError", "InternalServerError"}:
            category = "http_error"
        elif isinstance(exception, TypeError):
            category = "type_error"
        elif isinstance(exception, ValueError):
            category = "value_error"
        else:
            category = "unexpected"
        self.diagnostics = diagnostic_fields(stage, category, getattr(exception, "status_code", None),
                                             json_reason(exception) if category == "json_invalid" else None)
        # Preserve this existing stable pre-call limit sentinel, not arbitrary text.
        code = "AUTHORING_SOURCE_CONTEXT_LIMIT" if exception.args == ("AUTHORING_SOURCE_CONTEXT_LIMIT",) else "MODEL_FAILED"
        super().__init__(code)


@contextmanager
def failure_stage(stage):
    try:
        yield
    except AuthorFailure:
        raise
    except Exception as exception:
        raise AuthorFailure(stage, exception) from None