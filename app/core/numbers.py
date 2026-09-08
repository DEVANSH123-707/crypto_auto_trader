"""Decimal formatting shared by the API and the Binance client.

Money and quantities are ``Decimal`` everywhere in this project - never
``float``, because 0.1 has no exact binary representation and an order size
must round-trip byte for byte.

That leaves one presentation problem. ``NUMERIC(28, 12)`` pads on the way out,
so a quantity stored as ``0.001`` comes back from PostgreSQL as
``Decimal('0.001000000000')``. The two are numerically equal, but the padded
form is noise in an API response and is rejected outright by Binance if it ever
turned into scientific notation. :func:`format_decimal` produces the one
canonical rendering both callers want.
"""

from __future__ import annotations

from decimal import Decimal


def format_decimal(value: Decimal) -> str:
    """Render ``value`` in plain notation with no trailing zeros.

    ``Decimal('0.001000000000')`` -> ``'0.001'``
    ``Decimal('1.50')``           -> ``'1.5'``
    ``Decimal('100')``            -> ``'100'``

    Note the ``format(..., "f")``: ``Decimal.normalize()`` alone turns 0.001
    into ``1E-3``, and Binance rejects scientific notation.
    """
    if not value.is_finite():
        raise ValueError(f"Cannot format a non-finite Decimal: {value}")

    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"
