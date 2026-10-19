from pricing import order_total


def test_no_discount():
    assert order_total([(100, 1)]) == 120.0


def test_percentage_discount():
    assert order_total([(100, 1)], discount_pct=10) == 108.0


def test_discount_on_several_lines():
    assert order_total([(50, 2), (25, 4)], discount_pct=50) == 120.0
