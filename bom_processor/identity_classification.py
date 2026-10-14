"""Disabled-by-default forwarded-principal classifier; no storage or dispatch.

Input: Easy Auth headers/config. Output: booleans only, never identity values.
Run checks: python -m pytest tests/test_identity_classification.py.
"""


def classify_identity(headers, config, *, enabled=False):
    from deterministic_api import authorize, _decode_principal, _claim_values
    try:
        # Disabled means no identity detail even to otherwise allowed callers.
        if not enabled:
            return 404, {'error': {'code': 'not_found'}}
        if config.auth_mode != 'easy_auth':
            return 403, {'error': {'code': 'forbidden'}}
        encoded = headers.get('X-MS-CLIENT-PRINCIPAL', '')
        if not isinstance(encoded, str) or len(encoded) > 65536:
            return 401, {'error': {'code': 'unauthenticated'}}
        authorize(headers, config)
        principal = _decode_principal(encoded)
        applications = _claim_values(principal, {
            'appid', 'azp', 'http://schemas.microsoft.com/identity/claims/appid',
        }) & config.allowed_app_ids
        if len(applications) != 1:
            return 403, {'error': {'code': 'forbidden'}}
        app_only = _claim_values(principal, {'idtyp'}) == {'app'}
        delegated = bool(_claim_values(principal, {
            'scp', 'http://schemas.microsoft.com/identity/claims/scope',
        }))
        return 200, {'allowedApplicationMatch': True, 'appOnlyClaim': app_only,
                     'delegatedScopePresent': delegated,
                     'classificationCompatible': app_only and not delegated}
    except PermissionError:
        return 401, {'error': {'code': 'unauthenticated'}}
    except LookupError:
        return 403, {'error': {'code': 'forbidden'}}
    except Exception:
        return 500, {'error': {'code': 'classification_unavailable'}}