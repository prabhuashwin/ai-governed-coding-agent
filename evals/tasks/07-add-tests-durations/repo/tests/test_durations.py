from durations import parse_duration


def test_seconds_only():
    assert parse_duration("90s") == 90
