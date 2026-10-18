import pytest

from durations import parse_duration


@pytest.mark.parametrize("text,expected", [
    ("1h30m", 5400), ("45m", 2700), ("2h", 7200), ("1h2m3s", 3723), ("0s", 0), ("10m5s", 605),
])
def test_hidden_valid(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("bad", ["", "abc", "5x", "1h-2m", "-5m", "h", "1.5h"])
def test_hidden_invalid(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)
