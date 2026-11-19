"""Exact decimal USDC <-> integer micro-USDC conversion (1 USDC = 1_000_000 micro)."""

from decimal import Decimal, InvalidOperation

MICRO_PER_USDC = 1_000_000

FLOAT_AMOUNT_REFUSED = (
    'Pass the USDC amount as a STRING (e.g. "20.5") or integer micro-USDC (`amount_micro`). '
    "Binary floats cannot represent every 6-decimal USDC value exactly and are refused to prevent "
    "silent rounding errors on money operations."
)
_NOT_A_NUMBER = "Enter a valid positive USDC amount, e.g. '2.5'."
_TOO_MANY_PLACES = "USDC amounts carry at most 6 decimal places."
_NOT_POSITIVE = "The USDC amount must be greater than zero."


def parse_usdc_to_micro(raw: str | int | None) -> int:
    """Convert an exact decimal string or integer USDC amount to integer micro-USDC.

    Refuses binary ``float`` and ``bool`` values so money paths never silently round.
    """
    if isinstance(raw, bool):
        raise ValueError(_NOT_A_NUMBER)
    if isinstance(raw, float):
        raise ValueError(FLOAT_AMOUNT_REFUSED)
    try:
        amount = Decimal(str(raw if raw is not None else "").strip())
    except InvalidOperation as exc:
        raise ValueError(_NOT_A_NUMBER) from exc
    if not amount.is_finite():
        raise ValueError(_NOT_A_NUMBER)
    micro = amount * MICRO_PER_USDC
    if micro != micro.to_integral_value():
        raise ValueError(_TOO_MANY_PLACES)
    if micro <= 0:
        raise ValueError(_NOT_POSITIVE)
    return int(micro)


def resolve_usdc_micro(
    *, amount_micro: int | None = None, amount_usdc: str | int | None = None
) -> int:
    """Validate and resolve a mutually exclusive ``amount_micro`` / ``amount_usdc`` pair."""
    if (amount_micro is None) == (amount_usdc is None):
        raise ValueError(
            "Pass exactly one of amount_micro (integer micro-USDC) or "
            + "amount_usdc (exact decimal string, e.g. '10.5')."
        )
    if amount_micro is not None:
        if isinstance(amount_micro, bool):
            raise ValueError("amount_micro must be an integer number of micro-USDC.")
        if amount_micro <= 0:
            raise ValueError(_NOT_POSITIVE)
        return amount_micro
    return parse_usdc_to_micro(amount_usdc)


def micro_to_decimal(micro: int) -> Decimal:
    """Convert integer micro-USDC to an exact Decimal USDC amount."""
    return Decimal(micro) / Decimal(MICRO_PER_USDC)


def format_micro_usdc(micro: int) -> str:
    """Render integer micro-USDC cleanly without trailing zeroes (e.g. 2_500_000 -> '2.50')."""
    value = micro_to_decimal(micro).normalize()
    text = format(value, "f")
    if "." in text:
        whole, frac = text.split(".", 1)
        if len(frac) < 2:
            frac = frac.ljust(2, "0")
        return f"{whole}.{frac}"
    return f"{text}.00"
