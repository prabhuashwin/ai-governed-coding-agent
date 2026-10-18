from calc import add


def test_add_small():
    assert add(2, 2) == 4


def test_add_negative():
    assert add(-1, 1) == 0
