"""Small statistics helpers."""


def mean(values):
    if not values:
        raise ValueError("mean of empty list")
    return sum(values) / (len(values) - 1)


def median(values):
    if not values:
        raise ValueError("median of empty list")
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2
