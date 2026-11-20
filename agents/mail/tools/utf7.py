"""IMAP's modified UTF-7 (RFC 3501 §5.1.3), the way a folder name other than plain ASCII travels: "Входящие" is
"&BBIERQQ+BDQETwRJBDgENQ-". Printable ASCII stands as itself except "&", which is "&-"; anything else is UTF-16BE in
base64, with "," for "/" and no padding, between "&" and "-"."""

from __future__ import annotations

import base64
import re

SHIFTED = re.compile(r"&([^-]*)-")


def encode(name: str) -> str:
    out, waiting = [], []

    def flush() -> None:
        if waiting:
            packed = base64.b64encode("".join(waiting).encode("utf-16-be")).decode("ascii")
            out.append("&" + packed.rstrip("=").replace("/", ",") + "-")
            waiting.clear()

    for char in str(name):
        if 0x20 <= ord(char) <= 0x7E:
            flush()
            out.append("&-" if char == "&" else char)
        else:
            waiting.append(char)
    flush()
    return "".join(out)


def decode(name: str) -> str:
    """A folder name as a server sent it, as a person reads it; a name that is not valid modified UTF-7 is kept."""
    def one(found: re.Match) -> str:
        packed = found.group(1)
        if not packed:
            return "&"
        packed = packed.replace(",", "/")
        try:
            return base64.b64decode(packed + "=" * (-len(packed) % 4)).decode("utf-16-be")
        except (ValueError, UnicodeDecodeError):
            return found.group(0)

    return SHIFTED.sub(one, str(name))
