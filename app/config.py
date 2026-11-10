from decimal import Decimal
from pathlib import Path
from typing import Annotated

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    bot_token: SecretStr
    admin_ids: Annotated[list[int], NoDecode] = []

    shop_name: str = "Sakura Shop"
    database_url: str = "sqlite+aiosqlite:///data/shop.db"
    main_menu_photo: Path = Path("assets/main_menu.jpg")
    log_chat_id: int | None = None
    support_username: str | None = None

    referral_percent: int = 10
    min_deposit_rub: int = 10
    invoice_ttl_minutes: int = 30

    smm_api_url: str = "https://partner.soc-proof.su/api/v2"
    smm_api_key: SecretStr | None = None

    cryptobot_token: SecretStr | None = None
    cryptobot_testnet: bool = False

    # Rubles credited per 1 Telegram Star; 0 disables Stars payments.
    stars_rub_rate: Decimal = Decimal("0")

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _split_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return [int(part) for part in value.replace(";", ",").split(",") if part.strip()]
        if isinstance(value, int):
            return [value]
        return value

    @field_validator("support_username")
    @classmethod
    def _strip_at(cls, value: str | None) -> str | None:
        return value.lstrip("@") if value else value

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids
