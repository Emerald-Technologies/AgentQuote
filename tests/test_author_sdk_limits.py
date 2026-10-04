"""Offline actual-SDK author controls with fake HTTP transport only.

Requires installed Foundry SDK; run pytest here or execute run_check inside the
author image. No tokens requested or network access; response is synthetic429.
"""
import io
import json
import os
from types import SimpleNamespace
from unittest.mock import patch


def run_check():
    import httpx
    from openai import AsyncOpenAI, RateLimitError
    from agent_framework.exceptions import ChatClientException
    from azure.ai.projects.aio import AIProjectClient
    from openpyxl import Workbook
    from bom_processor.foundry_author import create_foundry_author

    requests, settings = [], []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(429, json={"error": {"message": "Synthetic rate limit", "type": "rate_limit"}})

    def client_factory(self, **kwargs):
        settings.append(kwargs)
        return AsyncOpenAI(api_key="offline-placeholder-not-a-credential", base_url="https://offline.invalid/v1",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kwargs)

    book = Workbook()
    book.active.append(["Assembly", "ASM-1"])
    stream = io.BytesIO()
    book.save(stream)
    book.close()
    prompt = SimpleNamespace(canonical_columns=("item_number",), rules=("write XLSX",))
    with patch.dict(os.environ, {"BOM_SOURCE_ASSOCIATIONS": "true",
                                "FOUNDRY_PROJECT_ENDPOINT": "https://offline.azure.us/api/projects/test"}), \
            patch.object(AIProjectClient, "get_openai_client", client_factory):
        author = create_foundry_author()
        try:
            author.generate_bundle(prompt, [{"sha256": "a"*64, "bytes": stream.getvalue(), "extension": "xlsx"}])
        except ChatClientException as exc:
            assert isinstance(exc.__cause__, RateLimitError)
        else:
            raise AssertionError("Expected synthetic rate-limit failure")
    assert len(settings) == 1 and settings[0]["max_retries"] == 0
    assert settings[0]["timeout"] == 120.0
    assert len(requests) == 1, "SDK must not retry the provider request"
    assert requests[0]["max_output_tokens"] == 8192
    return {"provider_attempts": len(requests), "max_output_tokens": requests[0]["max_output_tokens"],
            "max_retries": settings[0]["max_retries"], "timeout_seconds": settings[0]["timeout"]}


def test_actual_sdk_provider_request_is_bounded():
    import pytest
    pytest.importorskip("agent_framework_foundry")
    run_check()