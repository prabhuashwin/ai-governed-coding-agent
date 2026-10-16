"""Text helpers used by the web layer."""

import re


def slugify(text):
    text = text.lower()
    text = re.sub(r"[^a-z0-9]", "-", text)
    return text


def truncate(text, n):
    return text if len(text) <= n else text[: n - 1] + "…"
