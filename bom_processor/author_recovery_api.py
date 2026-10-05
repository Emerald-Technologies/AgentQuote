"""Explicit trusted-app recovery adapter; no polling or implicit authority.

Inputs: authenticated headers and bounded JSON. Output: safe recovery result.
Run: python -m pytest tests/test_author_recovery_api.py.
"""
import json

from .author_budget import BudgetError, _identifier, _require
from .package_writer_gate import PackageWriterBusy

MAX_BODY = 8192


class RecoveryApi:
    def __init__(self, config, service_factory, *, enabled=False):
        self.config, self.service_factory, self.enabled = config, service_factory, enabled

    def authenticate(self, headers):
        from deterministic_api import authorize, _decode_principal, _claim_values
        authorize(headers, self.config)
        # No local-disabled or human-object-ID substitute for app delegation.
        principal = _decode_principal(headers.get("X-MS-CLIENT-PRINCIPAL", ""))
        applications = _claim_values(principal, {
            "appid", "azp", "http://schemas.microsoft.com/identity/claims/appid",
        }) & self.config.allowed_app_ids
        if len(applications) != 1:
            raise LookupError("trusted application required")
        return next(iter(applications))

    def handle(self, headers, body):
        return self._handle(headers, body, lookup=False)

    def lookup(self, headers, body):
        return self._handle(headers, body, lookup=True)

    def _handle(self, headers, body, *, lookup):
        try:
            caller = self.authenticate(headers)
            if not self.enabled:
                return 403, {"error": {"code": "recovery_disabled"}}
            if len(body) > MAX_BODY:
                return 413, {"error": {"code": "request_too_large"}}
            value = json.loads(body.decode("utf-8"))
            _require(type(value) is dict and set(value) == {"operator_id", "recovery"})
            _identifier(value["operator_id"])
            service = self.service_factory()
            action = service.lookup if lookup else service.recover
            result = action(value["recovery"],
                actor_id=value["operator_id"], caller_application=caller)
            return 200, result
        except PermissionError:
            return 401, {"error": {"code": "unauthenticated"}}
        except LookupError:
            return 403, {"error": {"code": "forbidden"}}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 400, {"error": {"code": "invalid_json"}}
        except (BudgetError, PackageWriterBusy):
            return 409, {"error": {"code": "recovery_unavailable"}}
        except Exception:
            return 500, {"error": {"code": "recovery_failed"}}