"""Posts to a Mattermost incoming webhook. The sender is the only caller."""

import httpx


class MattermostClient:
    def __init__(self, webhook_url: str, *, timeout: float,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._webhook_url = webhook_url
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)

    @property
    def enabled(self) -> bool:
        """An empty webhook URL means no webhook yet: nothing is sent."""
        return self._webhook_url != ""

    async def post(self, text: str) -> None:
        """Raises on any failure, so the sender marks a message sent only once it really was."""
        response = await self._http.post(self._webhook_url, json={"text": text})
        response.raise_for_status()

    async def aclose(self) -> None:
        await self._http.aclose()
