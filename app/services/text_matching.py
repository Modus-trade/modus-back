import re

STOP = set("и в во на по для с со а но не у к из за что как это при нужно необходимо проблема проблемы сотрудник сотрудники".split())


def tokens(text):
    words = re.findall(r"[а-яa-z0-9]+", text.lower().replace("ё", "е"))
    return {re.sub(r"(иями|ами|ого|ему|ение|ения|ений|ать|ять|ить|ость|ости|ов|ам|ах|ый|ий|ая|ое|ые|ой|ом|ем|ть|ы|а|я|и|е|у)$", "", w)
            for w in words if len(w) > 2 and w not in STOP}
