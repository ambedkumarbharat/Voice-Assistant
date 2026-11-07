"""Streaming sentence segmenter: cuts LLM tokens into speakable chunks."""

from __future__ import annotations

import re

ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "eg", "ie",
    "etc", "inc", "ltd", "co", "no", "apt", "ave", "blvd", "rd",
}

_WORD_TAIL = re.compile(r"([A-Za-z.]+)$")


class SentenceSegmenter:
    def __init__(
        self,
        min_chars: int = 40,
        max_chars: int = 240,
        terminators: str = ".!?\n。！？…",
        clause_breaks: str = ",;:—–",
    ) -> None:
        self.buffer = ""
        self.min_chars = min_chars
        self.max_chars = max_chars
        self.terminators = set(terminators)
        self.clause_breaks = set(clause_breaks)

    def push(self, token: str) -> list[str]:
        if not token:
            return []
        self.buffer += token
        chunks: list[str] = []

        while True:
            index = self._find_terminator()
            if index < 0:
                break
            candidate = self.buffer[: index + 1].strip()
            self.buffer = self.buffer[index + 1 :].lstrip()
            if candidate:
                chunks.append(candidate)

        while len(self.buffer) > self.max_chars:
            window = self.buffer[: self.max_chars]
            cut = -1
            for i in range(len(window) - 1, self.min_chars - 1, -1):
                if window[i] in self.clause_breaks:
                    cut = i
                    break
            if cut < 0:
                cut = window.rfind(" ")
            if cut < 0:
                cut = self.max_chars - 1
            piece = self.buffer[: cut + 1].strip()
            self.buffer = self.buffer[cut + 1 :].lstrip()
            if piece:
                chunks.append(piece)

        return chunks

    def flush(self) -> list[str]:
        remaining = self.buffer.strip()
        self.buffer = ""
        return [remaining] if remaining else []

    def peek(self) -> str:
        return self.buffer

    def _find_terminator(self) -> int:
        for i, char in enumerate(self.buffer):
            if char not in self.terminators:
                continue
            if char == ".":
                before = self.buffer[max(0, i - 12) : i]
                match = _WORD_TAIL.search(before)
                word = (match.group(1).lower().rstrip(".") if match else "")
                if word in ABBREVIATIONS:
                    continue
                nxt = self.buffer[i + 1] if i + 1 < len(self.buffer) else ""
                if nxt.isdigit() and before and before[-1].isdigit():
                    continue
                if nxt and nxt not in " \t\n\"'”’)]":
                    continue
            return i
        return -1
