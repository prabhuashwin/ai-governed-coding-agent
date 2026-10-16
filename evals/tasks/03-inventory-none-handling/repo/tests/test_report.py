from report import Item, low_stock, total_value, unpriced_skus

ITEMS = [Item("A", 2, 10.0), Item("B", 1, None), Item("C", 3, 2.5), Item("D", 0, None)]


def test_total_skips_unpriced():
    assert total_value(ITEMS) == 27.5


def test_unpriced_skus():
    assert unpriced_skus(ITEMS) == ["B", "D"]


def test_low_stock():
    assert low_stock(ITEMS, threshold=2) == ["B", "D"]
