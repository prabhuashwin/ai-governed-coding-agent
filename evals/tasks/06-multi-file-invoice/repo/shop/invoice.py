"""Invoice totals."""

from .pricing import price_with_tax


def invoice_total(lines, discount_pct: float = 0.0) -> float:
    """lines: iterable of (quantity, net_unit_price). The discount applies to the net amount, before tax."""
    net = sum(q * p for q, p in lines)
    gross = price_with_tax(net)
    return round(gross - discount_pct, 2)
