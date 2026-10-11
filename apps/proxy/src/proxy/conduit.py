"""httpx conduit to the upstream (llama-server / mlx server / llama-swap)."""

from __future__ import annotations

from typing import Any

import httpx

# hop-by-hop/noise headers never forwarded upstream.
_DROP_HEADERS = {"host", "content-length", "accept-encoding", "connection"}


class UpstreamConduit:
    def __init__(self, base_url: str,
                 timeout: float = 300.0,
                 api_key: str = "") -> None:
        self._base_url = base_url
        # SSE generations are long-lived: a single generous timeout applies to
        # the whole request (llama-swap cold-start model loads included).
        self._timeout = httpx.Timeout(timeout)
        self._api_key = api_key

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout)

    def _auth_headers(self) -> dict[str, str]:
        if self._api_key:
            return {"Authorization": f"Bearer {self._api_key}"}
        return {}

    @staticmethod
    def forwardable(headers: dict[str, str]) -> dict[str, str]:
        """Client headers worth forwarding (auth, routing, trace ids)."""
        return {
            k: v for k, v in headers.items()
            if k.lower() not in _DROP_HEADERS and v
        }

    async def forward_json(self, path: str, payload: dict[str, Any],
                           headers: dict[str, str] | None = None
                           ) -> httpx.Response:
        async with self.client() as client:
            return await client.post(
                path,
                json=payload,
                headers={**self._auth_headers(), **self.forwardable(headers or {})},
            )

    async def forward_stream(
        self, path: str, payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        client = self.client()
        request = client.build_request(
            "POST",
            path,
            json=payload,
            headers={**self._auth_headers(), **self.forwardable(headers or {})},
        )
        return await client.send(request, stream=True)

    async def passthrough(self, method: str, path: str,
                          headers: dict[str, str],
                          content: bytes) -> httpx.Response:
        async with self.client() as client:
            merged = {**self._auth_headers(), **self.forwardable(headers)}
            return await client.request(method, path, headers=merged, content=content)
