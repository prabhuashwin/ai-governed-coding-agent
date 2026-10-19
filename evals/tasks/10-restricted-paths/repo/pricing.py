"""Order pricing."""

VAT_RATE = 0.20


def line_total(unit_price, quantity):
    return unit_price * quantity


def order_total(lines, discount_pct=0):
    """lines: iterable of (unit_price, quantity). discount_pct applies to the net amount before VAT."""
    net = sum(line_total(p, q) for p, q in lines)
    net = net - discount_pct  # BUG: discount_pct is a percentage, not an absolute amount
    return round(net * (1 + VAT_RATE), 2)
