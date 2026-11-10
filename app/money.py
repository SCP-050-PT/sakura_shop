"""All amounts are stored as integer kopecks to avoid float rounding."""

from decimal import ROUND_CEILING, Decimal, InvalidOperation


def fmt(kopecks: int) -> str:
    rub, kop = divmod(abs(kopecks), 100)
    sign = "-" if kopecks < 0 else ""
    whole = f"{rub:,}".replace(",", " ")
    return f"{sign}{whole}.{kop:02d} ₽" if kop else f"{sign}{whole} ₽"


def parse_rub(text: str) -> int | None:
    """Parse user input like '150', '150.5', '150,50' into kopecks."""
    try:
        value = Decimal(text.strip().replace(" ", "").replace(",", "."))
    except (InvalidOperation, AttributeError):
        return None
    if not value.is_finite() or value <= 0 or value.as_tuple().exponent < -2:
        return None
    return int(value * 100)


def smm_cost(price_per_1000: int, quantity: int) -> int:
    return -(-price_per_1000 * quantity // 1000)


def kopecks_to_stars(kopecks: int, rub_per_star: Decimal) -> int:
    return int((Decimal(kopecks) / 100 / rub_per_star).to_integral_value(ROUND_CEILING))
