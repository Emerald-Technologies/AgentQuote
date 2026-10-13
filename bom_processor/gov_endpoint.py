"""Azure Government model endpoint check shared by the model clients (PLM readiness D5, Nick 2026-10-12).

Customer BOMs can be export-controlled, so a model endpoint must be a clean https URL on a .azure.us
host. Input: the endpoint text. Output: True only for such a URL. Pure; no network.
"""
from __future__ import annotations

from urllib.parse import urlsplit


def is_gov_endpoint(endpoint: object) -> bool:
    if not isinstance(endpoint, str) or not endpoint or any(char.isspace() for char in endpoint):
        return False
    try:
        url = urlsplit(endpoint)
        return (url.scheme == "https" and url.hostname is not None and url.hostname.endswith(".azure.us")
                and url.port in {None, 443} and url.username is None and url.password is None
                and not url.query and not url.fragment)
    except ValueError:  # malformed host or port
        return False
