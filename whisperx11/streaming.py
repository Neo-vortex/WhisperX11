"""Early commit of stable words while a segment is still being spoken (LocalAgreement-2)."""

import re

_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)


def _norm(word):
    return _NON_WORD.sub("", word).lower()


def _same(a, b):
    return [_norm(w) for w in a] == [_norm(w) for w in b]


class SegmentAgreement:
    """Tracks successive previews of one growing segment.

    Words that two consecutive previews agree on are committed (typed right away), except the
    last `hold_back` words, which may still be cut off mid-word. When the final transcript of the
    segment arrives, only the remainder is typed; if the final text disagrees with what was
    committed, the committed text is erased with backspaces and replaced.
    """

    def __init__(self, hold_back=1):
        self.hold_back = hold_back
        self.prev = []
        self.committed = []
        self.typed = ""

    def preview(self, text):
        """Feed a preview transcript; returns new text to type now, or None."""
        words = text.split()
        agree = 0
        for a, b in zip(self.prev, words):
            if not _norm(a) or _norm(a) != _norm(b):
                break
            agree += 1
        self.prev = words
        upto = agree - self.hold_back
        k = len(self.committed)
        if upto <= k or not _same(words[:k], self.committed):
            return None
        new = words[k:upto]
        self.committed += new
        piece = " ".join(new) + " "
        self.typed += piece
        return piece

    def final(self, text):
        """Feed the final transcript; returns insert actions: ("text", s) / ("backspace", n)."""
        words = text.split()
        k = len(self.committed)
        if k and not _same(words[:k], self.committed):
            actions = [("backspace", len(self.typed))]
            return actions + ([("text", text + " ")] if text else [])
        rest = " ".join(words[k:])
        return [("text", rest + " ")] if rest else []
