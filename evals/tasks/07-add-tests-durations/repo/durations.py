"""Duration parsing for the scheduler."""


def parse_duration(text: str) -> int:
    """Parse strings like "1h30m", "45m", "2h", "90s" or "1h2m3s" into seconds.

    Raises ValueError for anything else (empty strings, unknown units, negative numbers, junk).
    """
    raise NotImplementedError
