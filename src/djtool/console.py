"""Console output (ANSI colors when available) and display helpers."""

from __future__ import annotations

import os
import sys


class Console:
    def __init__(self, color: bool | None = None):
        if color is None:
            color = (
                sys.stdout.isatty()
                and os.environ.get("NO_COLOR") is None
                and os.environ.get("TERM") != "dumb"
            )
        self.color = bool(color)

    def _style(self, code: str, text: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.color else text

    def bold(self, t: str) -> str:
        return self._style("1", t)

    def dim(self, t: str) -> str:
        return self._style("2", t)

    def red(self, t: str) -> str:
        return self._style("31", t)

    def green(self, t: str) -> str:
        return self._style("32", t)

    def yellow(self, t: str) -> str:
        return self._style("33", t)

    def cyan(self, t: str) -> str:
        return self._style("36", t)

    def magenta(self, t: str) -> str:
        return self._style("35", t)

    def out(self, text: str = "") -> None:
        print(text)

    def info(self, text: str) -> None:
        print(text)

    def warn(self, text: str) -> None:
        print(self.yellow("warning: ") + text)

    def error(self, text: str) -> None:
        print(self.red("error: ") + text, file=sys.stderr)

def fmt_duration(sec: float | None) -> str:
    if sec is None:
        return "unknown"
    total_tenths = max(0, round(sec * 10))
    minutes, rest = divmod(total_tenths, 600)
    seconds, tenths = divmod(rest, 10)
    return f"{minutes}:{seconds:02d}.{tenths}"


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} TiB"

