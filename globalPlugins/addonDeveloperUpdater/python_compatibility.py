"""Check add-on Python source against the Python version of its oldest supported NVDA."""

from __future__ import annotations

import ast, io, re, sys, tokenize, warnings

# NVDA's bundled Python changed at these releases (from NVDA's CI and
# .python-versions files): 2019.3 moved to 3.7, 2024.1 to 3.11, 2026.1 to 3.13.
_NVDA_PYTHON_VERSIONS = (((2026, 1), (3, 13)), ((2024, 1), (3, 11)), ((2019, 3), (3, 7)))
_OLDEST_CHECKED_PYTHON = (3, 7)


def python_version_for_nvda(nvda_version: str) -> tuple[int, int]:
    """Return the Python version bundled with an NVDA release, such as (3, 11) for 2025.3.3."""
    match = re.fullmatch(r"(20\d{2})\.(\d+)(?:\.\d+)?", str(nvda_version or "").strip())
    if not match:
        raise ValueError(f"Invalid NVDA version: {nvda_version}")
    release = (int(match.group(1)), int(match.group(2)))
    # NVDA releases before 2019.3 used Python 2, which cannot be checked here;
    # the oldest Python 3 grammar is the closest useful check.
    return next((python for first, python in _NVDA_PYTHON_VERSIONS if release >= first), _OLDEST_CHECKED_PYTHON)


def _newer_fstring_errors(source: str, target: tuple[int, int]) -> list[str]:
    """Find Python 3.12 f-string forms that ast.parse(feature_version=...) accepts.

    Before Python 3.12, an expression inside an f-string could not reuse the
    enclosing quote or contain a backslash.
    """
    if target >= (3, 12) or sys.version_info < (3, 12):
        return []
    errors = []; enclosing = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.FSTRING_START:
                if enclosing and enclosing[-1][1] > 0 and token.string.lstrip("rRbBfFuU").startswith(enclosing[-1][0]):
                    errors.append(f"line {token.start[0]}: f-string reuses its enclosing quote, which requires Python 3.12")
                enclosing.append([token.string.lstrip("rRbBfFuU"), 0])
            elif token.type == tokenize.FSTRING_END and enclosing:
                enclosing.pop()
            elif enclosing and token.type == tokenize.OP and token.string in "{}":
                enclosing[-1][1] += 1 if token.string == "{" else -1
            elif enclosing and enclosing[-1][1] > 0 and token.type == tokenize.STRING:
                quote = re.sub(r"^[rRbBuU]*", "", token.string)
                if quote.startswith(enclosing[-1][0]):
                    errors.append(f"line {token.start[0]}: f-string reuses its enclosing quote, which requires Python 3.12")
                elif "\\" in token.string:
                    errors.append(f"line {token.start[0]}: f-string expression contains a backslash, which requires Python 3.12")
    except (tokenize.TokenError, SyntaxError):
        return []
    return errors


def compatibility_errors(source: str, filename: str, target: tuple[int, int]) -> list[str]:
    """Return syntax errors for source that the target Python version could not load."""
    label = f"Python {target[0]}.{target[1]}"
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            ast.parse(source, filename=filename)
    except SyntaxError as error:
        return [f"{filename}: line {error.lineno}: {error.msg}"]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            ast.parse(source, filename=filename, feature_version=max(target, _OLDEST_CHECKED_PYTHON))
    except SyntaxError as error:
        return [f"{filename}: line {error.lineno}: uses syntax that {label} cannot load ({error.msg})"]
    return [f"{filename}: {error}; {label} cannot load it" for error in _newer_fstring_errors(source, target)]
