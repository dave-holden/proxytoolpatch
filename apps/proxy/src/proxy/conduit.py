"""httpx conduit to the upstream (llama-server / mlx server / llama-swap)."""

from __future__ import annotations

from typing import Any

import httpx


class UpstreamConduit:
    def __init__(self, base_url: str) -> None:
        self._base_url = base_url
        self._timeout = httpx.Timeout(300.0)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout)

    async def forward_json(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        async with self.client() as client:
            return await client.post(path, json=payload)

    async def forward_stream(
        self, path: str, payload: dict[str, Any]
    ) -> httpx.Response:
        client = self.client()
        request = client.build_request("POST", path, json=payload)
        return await client.send(request, stream=True)

    async def passthrough(self, method: str, path: str,
                          headers: dict[str, str],
                          content: bytes) -> httpx.Response:
        async with self.client() as client:
            return await client.request(
                method, path, headers=headers, content=content
            )
