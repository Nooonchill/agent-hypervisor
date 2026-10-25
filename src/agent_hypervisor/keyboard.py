from __future__ import annotations

from functools import wraps

from textual import events
from textual._xterm_parser import XTermParser


def preserve_alt_with_associated_text() -> None:
    # Textual 8 discards Alt when Kitty includes text. Restore it only for
    # unchanged characters: AltGr-generated text must remain ordinary input.
    parse = getattr(XTermParser, "_parse_extended_key", None)
    if parse is None or getattr(parse, "_hypervisor_alt", False):
        return

    @wraps(parse)
    def parse_extended_key(self: XTermParser, sequence: str) -> list[events.Key] | None:
        keys = parse(self, sequence)
        if not keys:
            return keys
        fields = sequence[2:-1].split(";")
        if len(fields) < 3 or not fields[2]:
            return keys
        codepoint = int(fields[0].split(":")[0] or "1")
        if codepoint == 27 and sequence.endswith("~"):
            codepoint = int(fields[2].split(":")[0])  # xterm modifyOtherKeys
        modifiers = int(fields[1].split(":")[0] or "1") - 1
        if not modifiers & 2 or modifiers & 4:
            return keys
        character = chr(codepoint)
        return [
            events.Key(f"alt+{key.key}", key.character)
            if key.character is not None
            and key.character.casefold() == character.casefold()
            and "alt" not in key.key.split("+")
            else key
            for key in keys
        ]

    parse_extended_key._hypervisor_alt = True
    XTermParser._parse_extended_key = parse_extended_key
