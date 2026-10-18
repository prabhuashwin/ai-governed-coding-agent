"""Split a command string into argv without a shell, on every OS.

POSIX `shlex.split` treats backslashes as escapes, which destroys Windows paths
(`C:\\Python\\python.exe` -> `C:Pythonpython.exe`). On Windows we use non-POSIX mode and strip the
surrounding quotes it leaves on tokens.
"""

from __future__ import annotations

import os
import shlex


def split_command(s: str, *, windows: bool | None = None) -> list[str]:
    windows = (os.name == "nt") if windows is None else windows
    if not windows:
        return shlex.split(s)
    out = []
    for tok in shlex.split(s, posix=False):
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
            tok = tok[1:-1]
        out.append(tok)
    return out
