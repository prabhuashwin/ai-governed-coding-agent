"""Unit pricing."""

from .rates import VAT_RATE


def price_with_tax(net: float) -> float:
    """Gross price for a net amount, rounded to cents."""
    return round(net * VAT_RATE, 2)
