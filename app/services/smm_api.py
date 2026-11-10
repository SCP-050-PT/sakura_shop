"""Client for SMM panels speaking the common "API v2" protocol (soc-proof and most PerfectPanel clones)."""

from dataclasses import dataclass
from typing import Any

import httpx


class SmmApiError(Exception):
    pass


class SmmTransportError(SmmApiError):
    """No usable answer from the panel: the request may or may not have been executed."""


@dataclass(slots=True)
class SmmOrderStatus:
    status: str
    remains: int | None
    charge: str | None


class SmmPanelClient:
    def __init__(self, api_url: str, api_key: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._url = api_url
        self._key = api_key
        self._client = client or httpx.AsyncClient(timeout=20)

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, action: str, **params: Any) -> Any:
        try:
            response = await self._client.post(self._url, data={"key": self._key, "action": action, **params})
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SmmTransportError(f"SMM panel request failed: {exc}") from exc
        if isinstance(payload, dict) and payload.get("error"):
            raise SmmApiError(str(payload["error"]))
        return payload

    async def add_order(self, service_id: str, link: str, quantity: int) -> str:
        payload = await self._call("add", service=service_id, link=link, quantity=quantity)
        order_id = payload.get("order") if isinstance(payload, dict) else None
        if order_id is None:
            raise SmmApiError(f"Unexpected response: {payload!r}")
        return str(order_id)

    async def order_status(self, order_id: str) -> SmmOrderStatus:
        payload = await self._call("status", order=order_id)
        if not isinstance(payload, dict) or "status" not in payload:
            raise SmmApiError(f"Unexpected response: {payload!r}")
        remains = payload.get("remains")
        return SmmOrderStatus(
            status=str(payload["status"]),
            remains=int(remains) if remains not in (None, "") else None,
            charge=payload.get("charge"),
        )

    async def balance(self) -> str:
        payload = await self._call("balance")
        return f"{payload.get('balance')} {payload.get('currency', '')}".strip()

    async def services(self) -> list[dict[str, Any]]:
        payload = await self._call("services")
        if not isinstance(payload, list):
            raise SmmApiError(f"Unexpected response: {payload!r}")
        return payload
