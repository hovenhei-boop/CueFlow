from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from cueflow.errors import ContractError


@dataclass(frozen=True)
class TextChange:
    start: int
    end: int
    variant_start: int
    variant_end: int
    replacement: str


@dataclass(frozen=True)
class TextMap:
    base: str
    variant: str
    opcodes: tuple[tuple[str, int, int, int, int], ...]

    @property
    def changes(self) -> tuple[TextChange, ...]:
        return tuple(
            TextChange(a, b, c, d, self.variant[c:d])
            for tag, a, b, c, d in self.opcodes
            if tag != "equal"
        )

    def boundary(self, position: int, *, after_insert: bool = False) -> int | None:
        """Map exact boundaries only. Never interpolate through an opaque replacement."""
        if not 0 <= position <= len(self.base):
            raise ContractError("Text coordinate is outside frozen Base")
        points: list[int] = []
        for tag, a, b, c, d in self.opcodes:
            if a == position:
                points.append(c)
            if b == position:
                points.append(d)
            if a < position < b:
                return c + position - a if tag == "equal" else None
        if not points:
            return 0 if not self.base and not self.variant else None
        return max(points) if after_insert else min(points)

    def interval(
        self, start: int, end: int, *, include_end_insert: bool = False
    ) -> tuple[int, int] | None:
        if start > end:
            raise ContractError("Reversed text interval")
        left = self.boundary(start)
        right = self.boundary(end, after_insert=include_end_insert)
        if left is None or right is None:
            return None
        return left, right


def build_text_map(base: str, variant: str) -> TextMap:
    """Raw Unicode codepoint coordinates; no normalization or fuzzy reconstruction."""
    opcodes = tuple(SequenceMatcher(a=base, b=variant, autojunk=False).get_opcodes())
    result = TextMap(base, variant, opcodes)
    cursor = 0
    rebuilt: list[str] = []
    for change in result.changes:
        rebuilt.extend((base[cursor : change.start], change.replacement))
        cursor = change.end
    rebuilt.append(base[cursor:])
    if "".join(rebuilt) != variant:
        raise ContractError("Full transcript diff did not round-trip exactly")
    return result
