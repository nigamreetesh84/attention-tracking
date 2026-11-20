"""Pseudo-locale generated from English, for layout and hard-coded-string checks.

"Scan recorded." becomes "[Šçàñ ŕéçöŕđéđ. ~~~~]": accented so untranslated
text stands out, padded so layouts see longer strings, bracketed so clipping
is visible. Placeholders are left untouched.
"""

import string

_MAP = str.maketrans(
    "AEIOUYaeiouycnsdrzlgtCNSDRZLGT",
    "ÀÉÎÕÜÝàéîõüýçñšđŕžłğŧÇÑŠĐŔŽŁĞŦ",
)


def pseudo_localize(template: str) -> str:
    """Pseudo-localized `template` with every placeholder preserved."""
    out: list[str] = []
    for literal, name, spec, conv in string.Formatter().parse(template):
        out.append(literal.translate(_MAP).replace("{", "{{").replace("}", "}}"))
        if name is not None:
            out.append(
                "{" + name + (f"!{conv}" if conv else "") + (f":{spec}" if spec else "") + "}"
            )
    body = "".join(out)
    pad = "~" * max(2, len(template) // 3)
    return f"[{body} {pad}]"
