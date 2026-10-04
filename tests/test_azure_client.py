"""Azure HTTP client — mocked with respx (no real API)."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.azure_client import classify_batch


@respx.mock
async def test_classify_batch_success(settings):
    route = respx.post(settings.azure_classify_url).mock(
        return_value=httpx.Response(200, json={"Data": [{"purchaseorderid": 1}]})
    )
    body = await classify_batch({"Data": []}, settings)
    assert body["Data"][0]["purchaseorderid"] == 1
    assert route.called


@respx.mock
async def test_classify_batch_http_error(settings):
    respx.post(settings.azure_classify_url).mock(
        return_value=httpx.Response(400, text="bad request")
    )
    with pytest.raises(RuntimeError, match="HTTP 400"):
        await classify_batch({"Data": []}, settings)


@respx.mock
async def test_classify_batch_retries_then_fails(settings, monkeypatch):
    monkeypatch.setattr(settings, "azure_max_retries", 2)
    monkeypatch.setattr(settings, "azure_retry_backoff_seconds", 0)
    respx.post(settings.azure_classify_url).mock(
        return_value=httpx.Response(503, text="busy")
    )
    with pytest.raises(RuntimeError, match="HTTP 503"):
        await classify_batch({"Data": []}, settings)


async def test_classify_batch_missing_url(settings, monkeypatch):
    monkeypatch.setattr(settings, "azure_classify_url", "")
    with pytest.raises(RuntimeError, match="not configured"):
        await classify_batch({"Data": []}, settings)
