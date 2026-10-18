import pytest

from stats import mean, median


def test_mean():
    assert mean([2, 4, 6]) == 4.0


def test_mean_single():
    assert mean([7]) == 7.0


def test_mean_empty():
    with pytest.raises(ValueError):
        mean([])


def test_median():
    assert median([3, 1, 2]) == 2
    assert median([4, 1, 3, 2]) == 2.5
