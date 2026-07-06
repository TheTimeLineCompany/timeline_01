"""OpenAI-compatible vLLM client.

This client is provided for background workers only. Reader APIs must not call
it on their critical path.
"""

from typing import Any

import httpx

from app.core.config import Settings


class LocalLLMClient:
    """Small async client for the local vLLM endpoint."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = settings.llm_base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {settings.llm_api_key}",
            "Content-Type": "application/json",
        }
        self._client: httpx.AsyncClient | None = None

    async def aclose(self) -> None:
        """Close the pooled HTTP client if it was opened."""

        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http_client(self) -> httpx.AsyncClient:
        timeout = self._bounded_timeout(None)
        if not self.settings.llm_keepalive:
            return httpx.AsyncClient(timeout=timeout)
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=timeout)
        return self._client

    def _bounded_timeout(self, requested: float | None) -> float:
        base = requested or self.settings.llm_timeout_seconds
        hard = max(1.0, float(self.settings.llm_hard_timeout_seconds))
        return max(1.0, min(float(base), hard))

    async def list_models(self) -> dict[str, Any]:
        """Return raw `/models` response."""

        client = self._http_client()
        response = await client.get(
            f"{self.base_url}/models",
            headers=self.headers,
            timeout=self._bounded_timeout(None),
        )
        if not self.settings.llm_keepalive:
            await client.aclose()
            response.raise_for_status()
        response.raise_for_status()
        return response.json()

    async def health_check(self) -> bool:
        """Return whether vLLM is reachable."""

        try:
            await self.list_models()
        except Exception:
            return False
        return True

    async def chat_completion(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.1,
        max_tokens: int = 900,
        response_format: dict[str, Any] | None = None,
        guided_json: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Call `/chat/completions`."""

        payload = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        if guided_json is not None:
            payload["guided_json"] = guided_json

        client = self._http_client()
        try:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=self.headers,
                json=payload,
                timeout=self._bounded_timeout(timeout_seconds),
            )
            if response.is_error:
                detail = response.text[:1200]
                raise httpx.HTTPStatusError(
                    f"{response.status_code} from local LLM: {detail}",
                    request=response.request,
                    response=response,
                )
            return response.json()
        except httpx.TimeoutException as exc:
            raise TimeoutError(
                f"Local LLM request exceeded {self._bounded_timeout(timeout_seconds):.1f}s timeout"
            ) from exc
        finally:
            if not self.settings.llm_keepalive:
                await client.aclose()
