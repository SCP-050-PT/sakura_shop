"""Minimal Crypto Pay API client (https://help.crypt.bot/crypto-pay-api)."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

MAINNET_URL = "https://pay.crypt.bot/api"
TESTNET_URL = "https://testnet-pay.crypt.bot/api"


class CryptoBotError(Exception):
    pass


@dataclass(slots=True)
class CryptoInvoice:
    invoice_id: int
    status: str
    url: str


class CryptoBotClient:
    def __init__(self, token: str, *, testnet: bool = False, client: httpx.AsyncClient | None = None) -> None:
        self._base = TESTNET_URL if testnet else MAINNET_URL
        self._client = client or httpx.AsyncClient(timeout=20)
        self._headers = {"Crypto-Pay-API-Token": token}

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, params: dict[str, Any]) -> Any:
        try:
            response = await self._client.post(f"{self._base}/{method}", json=params, headers=self._headers)
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise CryptoBotError(f"Crypto Pay request failed: {exc}") from exc
        if not payload.get("ok"):
            raise CryptoBotError(f"Crypto Pay error: {payload.get('error')}")
        return payload["result"]

    @staticmethod
    def _invoice(data: dict[str, Any]) -> CryptoInvoice:
        return CryptoInvoice(
            invoice_id=int(data["invoice_id"]),
            status=str(data["status"]),
            url=data.get("bot_invoice_url") or data.get("pay_url") or "",
        )

    async def create_invoice(
        self, amount_kopecks: int, *, description: str, payload: str, expires_in: int
    ) -> CryptoInvoice:
        result = await self._call(
            "createInvoice",
            {
                "currency_type": "fiat",
                "fiat": "RUB",
                "amount": str(Decimal(amount_kopecks) / 100),
                "description": description[:1024],
                "payload": payload,
                "expires_in": expires_in,
            },
        )
        return self._invoice(result)

    async def get_invoices(self, invoice_ids: list[int]) -> dict[int, CryptoInvoice]:
        if not invoice_ids:
            return {}
        result = await self._call("getInvoices", {"invoice_ids": ",".join(map(str, invoice_ids))})
        return {inv.invoice_id: inv for inv in map(self._invoice, result.get("items", []))}
