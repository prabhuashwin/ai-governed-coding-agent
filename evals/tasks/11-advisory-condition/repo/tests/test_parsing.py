import pytest

from parsing import parse_setting


def test_simple():
    assert parse_setting("name=alice") == ("name", "alice")


def test_strips_whitespace():
    assert parse_setting(" name = alice ") == ("name", "alice")


def test_value_may_contain_equals():
    assert parse_setting("token=abc==") == ("token", "abc==")


def test_missing_equals_raises():
    with pytest.raises(ValueError):
        parse_setting("no separator here")
