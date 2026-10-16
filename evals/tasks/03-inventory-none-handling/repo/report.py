"""Inventory valuation."""

from dataclasses import dataclass


@dataclass
class Item:
    sku: str
    qty: int
    price: float | None


def total_value(items):
    return sum(i.qty * i.price for i in items)


def low_stock(items, threshold=5):
    return [i.sku for i in items if i.qty < threshold]
