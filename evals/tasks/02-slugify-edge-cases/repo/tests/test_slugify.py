from textutil import slugify, truncate


def test_basic():
    assert slugify("Hello World") == "hello-world"


def test_collapses_runs():
    assert slugify("Hello,   World!!!") == "hello-world"


def test_strips_edges():
    assert slugify("--Hello World--") == "hello-world"


def test_empty_and_none():
    assert slugify("") == ""
    assert slugify(None) == ""


def test_truncate():
    assert truncate("abcdef", 4) == "abc…"
    assert truncate("abc", 4) == "abc"
