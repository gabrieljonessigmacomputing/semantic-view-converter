"""Display names. Same rules as the replica builder: first synonym, else a humanized identifier."""

import re

SUFFIX_WORDS = [
    "DATE", "ID", "NAME", "AMOUNT", "NUMBER", "TYPE", "STATUS", "CODE",
    "COUNT", "TOTAL", "BALANCE", "SECONDS", "MINUTES",
]
SMALL = {"of", "to", "and", "or", "the", "a", "an", "in", "on", "for", "by", "per", "vs"}
ACRONYMS = {
    "id": "ID", "arr": "ARR", "mrr": "MRR", "csm": "CSM", "nps": "NPS", "sfdc": "SFDC",
    "ar": "AR", "gtm": "GTM", "url": "URL", "ai": "AI", "cx": "CX", "acv": "ACV",
    "tcv": "TCV", "ae": "AE", "ytd": "YTD", "qtd": "QTD", "mtd": "MTD", "naics": "NAICS",
    "sic": "SIC", "pnc": "PNC", "eb": "EB", "rnd": "R&D", "bkb": "BKB", "cr": "CR",
    "ps": "PS", "us": "US", "utc": "UTC",
}


def smart_title(text):
    out = []
    for i, word in enumerate(str(text).split()):
        low = word.lower()
        if low in ACRONYMS and not any(ch.isupper() for ch in word[1:]):
            out.append(ACRONYMS[low])
        elif any(ch.isupper() for ch in word):
            out.append(word[0].upper() + word[1:])
        elif i and low in SMALL:
            out.append(low)
        elif word:
            out.append(word[0].upper() + word[1:])
    return " ".join(out)


def humanize(name):
    cleaned = re.sub(r"^[A-Z0-9]+__", "", str(name))
    cleaned = re.sub(r"__C$", "", cleaned)
    words = []
    for word in [part for part in cleaned.split("_") if part]:
        for suffix in SUFFIX_WORDS:
            if word.upper().endswith(suffix) and len(word) > len(suffix) + 2:
                word = word[: -len(suffix)] + " " + suffix
                break
        words.extend(word.split())
    return smart_title(" ".join(part.lower() for part in words))


def column_name(logical_name, synonyms):
    if synonyms:
        return smart_title(synonyms[0])
    return humanize(logical_name)


def metric_name(logical_name):
    """Metrics keep the humanized logical name. Synonyms go in the description."""
    return humanize(logical_name)


def also_called(synonyms):
    if not synonyms:
        return ""
    return " Also called: " + ", ".join(synonyms) + "."


def slug(text):
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")
