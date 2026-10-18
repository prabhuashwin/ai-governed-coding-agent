from shop.invoice import invoice_total
from shop.pricing import price_with_tax


def test_price_with_tax():
    assert price_with_tax(100) == 120.0


def test_invoice_total_no_discount():
    assert invoice_total([(2, 10.0), (1, 5.0)]) == 30.0


def test_invoice_total_with_discount():
    assert invoice_total([(1, 100.0)], discount_pct=10) == 108.0
