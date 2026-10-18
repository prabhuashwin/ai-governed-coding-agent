from shop.invoice import invoice_total
from shop.pricing import price_with_tax


def test_hidden_rounding():
    assert price_with_tax(0.1) == 0.12


def test_hidden_empty_invoice():
    assert invoice_total([]) == 0.0


def test_hidden_large_discount_and_quantities():
    assert invoice_total([(3, 19.99), (10, 0.5)], discount_pct=25) == 58.47


def test_hidden_zero_discount_default():
    assert invoice_total([(1, 1.0)]) == 1.2
