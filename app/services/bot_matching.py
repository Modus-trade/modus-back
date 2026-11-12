"""Similarity for the chatbot; website knowledge workflows remain independent."""
from difflib import SequenceMatcher
from .text_matching import tokens
from .problems import affected_count

ACTIVE = ("NEW", "IN_REVIEW", "IN_PROGRESS", "AWAITING_CLARIFICATION")


def similarity(text, title):
    left, right = tokens(text), tokens(title)
    common = left & right
    if len(common) < 2:
        return 1.0 if text.strip().casefold() == title.strip().casefold() else 0
    coverage = len(common) / max(1, len(right))
    jaccard = len(common) / max(1, len(left | right))
    sequence = SequenceMatcher(None, text.lower(), title.lower()).ratio()
    return .55 * coverage + .3 * jaccard + .15 * sequence
