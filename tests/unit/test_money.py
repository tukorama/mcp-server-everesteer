"""Unit tests for exact decimal USDC <-> micro-USDC conversion."""

from decimal import Decimal
from typing import Any

import pytest

from mcp_server_everesteer.money import (
    format_micro_usdc,
    micro_to_decimal,
    parse_usdc_to_micro,
    resolve_usdc_micro,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", 1_000_000),
        ("2.5", 2_500_000),
        ("0.000001", 1),
        (10, 10_000_000),
        (" 42.123456 ", 42_123_456),
    ],
)
def test_valid_usdc_to_micro(raw: str | int, expected: int) -> None:
    assert parse_usdc_to_micro(raw) == expected


@pytest.mark.parametrize(
    "bad",
    [
        2.5,
        0.1,
        True,
        False,
        None,
        "",
        "0",
        "-5",
        "1.1234567",
        "NaN",
        "Infinity",
        "abc",
    ],
)
def test_refuses_floats_bools_and_invalid_usdc(bad: Any) -> None:
    with pytest.raises(ValueError, match="USDC"):
        parse_usdc_to_micro(bad)


def test_resolve_usdc_micro_mutually_exclusive() -> None:
    assert resolve_usdc_micro(amount_micro=500_000) == 500_000
    assert resolve_usdc_micro(amount_usdc="0.5") == 500_000
    with pytest.raises(ValueError, match="Pass exactly one"):
        resolve_usdc_micro()
    with pytest.raises(ValueError, match="Pass exactly one"):
        resolve_usdc_micro(amount_micro=100, amount_usdc="1")


def test_formatting_helpers() -> None:
    assert micro_to_decimal(2_500_000) == Decimal("2.5")
    assert format_micro_usdc(2_500_000) == "2.50"
    assert format_micro_usdc(1) == "0.000001"
