"""Call the deployed Azure classify-po API."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


async def classify_batch(payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """
    POST one batch to Azure with retries on 429 / 5xx / network errors.
    Raises RuntimeError after retries are exhausted (short messages only).
    """
    if not settings.azure_classify_url:
        raise RuntimeError("Azure classifier URL is not configured.")

    headers = {"Content-Type": "application/json"}
    if settings.azure_function_key:
        headers["x-functions-key"] = settings.azure_function_key

    url = settings.azure_classify_url
    last_error = "Azure classification failed"
    backoff = settings.azure_retry_backoff_seconds

    async with httpx.AsyncClient(timeout=settings.azure_timeout_seconds) as client:
        for attempt in range(1, settings.azure_max_retries + 1):
            try:
                response = await client.post(url, json=payload, headers=headers)
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = f"Azure classification failed (HTTP {response.status_code})"
                    logger.warning(
                        "Azure retryable error status=%s attempt=%s body=%s",
                        response.status_code,
                        attempt,
                        response.text[:500],
                    )
                    if attempt < settings.azure_max_retries:
                        await asyncio.sleep(backoff * attempt)
                        continue
                    raise RuntimeError(last_error)

                if response.status_code >= 400:
                    logger.error(
                        "Azure client error status=%s body=%s",
                        response.status_code,
                        response.text[:800],
                    )
                    raise RuntimeError(
                        f"Azure classification failed (HTTP {response.status_code})"
                    )

                body = response.json()
                if not isinstance(body, dict):
                    raise RuntimeError("Azure returned an invalid response.")
                return body
            except httpx.TimeoutException:
                last_error = "Azure request timed out"
                logger.warning("Azure timeout attempt=%s", attempt)
                if attempt < settings.azure_max_retries:
                    await asyncio.sleep(backoff * attempt)
                    continue
                raise RuntimeError(last_error) from None
            except httpx.HTTPError as exc:
                last_error = "Could not reach Azure classifier"
                logger.warning("Azure network error attempt=%s err=%s", attempt, exc)
                if attempt < settings.azure_max_retries:
                    await asyncio.sleep(backoff * attempt)
                    continue
                raise RuntimeError(last_error) from None
            except RuntimeError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("Unexpected Azure client error")
                raise RuntimeError("Azure classification failed") from exc

    raise RuntimeError(last_error)
