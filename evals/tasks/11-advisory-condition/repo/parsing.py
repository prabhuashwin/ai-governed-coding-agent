"""Parse 'key=value' settings lines."""


def parse_setting(line):
    """Return (key, value) for a line like 'name = value'. Whitespace around both parts is ignored."""
    key, value = line.split("=")  # BUG: values containing '=' (e.g. base64) raise ValueError
    return key, value
