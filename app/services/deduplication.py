"""Local lexical similarity. Only opt-in public titles are searched."""
import re
from difflib import SequenceMatcher
from sqlalchemy import func, select
from ..models import Ticket, TicketParticipant

ACTIVE = ("NEW", "IN_REVIEW", "IN_PROGRESS", "AWAITING_CLARIFICATION")
STOP = set("и в во на по для с со а но не у к из за что как это при нужно необходимо проблема проблемы сотрудник сотрудники".split())


def affected_count():
    return 1 + (select(func.count()).select_from(TicketParticipant)
        .where(TicketParticipant.ticket_id == Ticket.id, TicketParticipant.user_id != Ticket.author_id)
        .correlate(Ticket).scalar_subquery())


def tokens(text):
    words = re.findall(r"[а-яa-z0-9]+", text.lower().replace("ё", "е"))
    return {re.sub(r"(иями|ами|ого|ему|ение|ения|ений|ать|ять|ить|ость|ости|ов|ам|ах|ый|ий|ая|ое|ые|ой|ом|ем|ть|ы|а|я|и|е|у)$", "", w)
            for w in words if len(w) > 2 and w not in STOP}


def similarity(text, title):
    left, right = tokens(text), tokens(title)
    common = left & right
    if len(common) < 2:
        return 1.0 if text.strip().casefold() == title.strip().casefold() else 0
    coverage = len(common) / max(1, len(right))
    jaccard = len(common) / max(1, len(left | right))
    sequence = SequenceMatcher(None, text.lower(), title.lower()).ratio()
    return .55 * coverage + .3 * jaccard + .15 * sequence
