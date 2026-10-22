"""Parse a Snowflake semantic view (CREATE SEMANTIC VIEW, or Cortex YAML) into one dict.

Item syntax is `<table>.<LOGICAL_NAME> AS <PHYSICAL_EXPR>`. The logical name comes first.
"""

import json
import re

import yaml

SECTION_KW = ("tables", "relationships", "facts", "dimensions", "metrics")


def _scan(text):
    depth, quoted, i = 0, False, 0
    while i < len(text):
        ch = text[i]
        if quoted:
            if ch == "'" and i + 1 < len(text) and text[i + 1] == "'":
                yield i, ch, depth, True
                i += 2
                continue
            if ch == "'":
                quoted = False
            yield i, ch, depth, True
        else:
            if ch == "'":
                quoted = True
                yield i, ch, depth, True
            else:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                yield i, ch, depth, False
        i += 1


def _sections(text):
    """Top-level sections, in whatever order the statement writes them."""
    out = {}
    end = 0
    pattern = re.compile(r"(?<![\w.])(" + "|".join(SECTION_KW) + r")\s*\(", re.I)
    for match in pattern.finditer(text):
        depth = 0
        for _, _, d, _ in _scan(text[: match.start()]):
            depth = d
        if depth != 0:
            continue
        kw = match.group(1).lower()
        if kw in out:
            continue
        start = match.end()
        for i, ch, d, quoted in _scan(text[start:]):
            if not quoted and ch == ")" and d == -1:
                out[kw] = text[start : start + i]
                end = max(end, start + i + 1)
                break
    return out, text[end:]


def _split_top(body, sep=","):
    parts, start = [], 0
    for i, ch, depth, quoted in _scan(body):
        if not quoted and depth == 0 and ch == sep:
            parts.append(body[start:i])
            start = i + 1
    parts.append(body[start:])
    return [part.strip() for part in parts if part.strip()]


def _unq(text):
    text = text.strip()
    if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
        return text[1:-1].replace("''", "'")
    return text


def _clean_description(text):
    """Drop the sample_values / is_enum tail the DDL leaves after a comment, and keep the samples."""
    if not text:
        return None
    text = text.strip()
    samples = []
    match = re.search(r"\bsample_values\s*\((.*)\)\s*(?:is_enum\s*)?$", text, re.I | re.S)
    if match:
        samples = [_unq(part.strip()) for part in _split_top(match.group(1))]
        text = text[: match.start()].strip()
    text = re.sub(r"\bis_enum\s*$", "", text, flags=re.I).strip()
    if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
        text = text[1:-1].replace("''", "'")
    text = text.strip()
    if samples:
        prefix = (text.rstrip(".") + ". ") if text else ""
        text = prefix + "Values: " + ", ".join(samples) + "."
    return text or None


def _cut_clauses(item):
    comment = None
    cpos = None
    clen = 0
    for i, _, depth, quoted in _scan(item):
        if quoted or depth != 0:
            continue
        if i and (item[i - 1].isalnum() or item[i - 1] == "_"):
            continue
        match = re.match(r"comment\s*=\s*", item[i:], re.I)
        if match:
            cpos = i
            clen = match.end()
    if cpos is not None:
        comment = item[cpos + clen :]
        item = item[:cpos].rstrip()
    synonyms = []
    spos = None
    for i, _, depth, quoted in _scan(item):
        if not quoted and depth == 0 and re.match(r"with\s+synonyms\s*=", item[i:], re.I) and (i == 0 or not item[i - 1].isalnum()):
            spos = i
    if spos is not None:
        sm = re.match(r"with\s+synonyms\s*=\s*\((.*)\)\s*$", item[spos:], re.I | re.S)
        if sm:
            synonyms = [_unq(part) for part in _split_top(sm.group(1))]
        item = item[:spos].rstrip()
    return item.strip(), synonyms, _clean_description(comment)


def _verified_queries(text):
    found = text.lower().find("ai_verified_queries")
    if found < 0:
        return []
    body = text[found:]
    pattern = re.compile(
        r"(\"[^\"]+\"|\w+)\s+AS\s*\(\s*QUESTION\s+'((?:[^']|'')*)'\s*(?:ONBOARDING_QUESTION\s+(\w+)\s*)?SQL\s+'((?:[^']|'')*)'\s*\)",
        re.I | re.S,
    )
    out = []
    for match in pattern.finditer(body):
        out.append({
            "name": match.group(1).strip('"'),
            "question": match.group(2).replace("''", "'"),
            "onboarding": (match.group(3) or "").lower() == "true",
            "sql": match.group(4).replace("''", "'"),
        })
    return out


def _named_filters(tail):
    match = re.search(r"with\s+extension\s*\(\s*CA\s*=\s*'", tail, re.I)
    if not match:
        return []
    start = match.end()
    chars = []
    i = start
    while i < len(tail):
        if tail[i] == "'" and i + 1 < len(tail) and tail[i + 1] == "'":
            chars.append("'")
            i += 2
            continue
        if tail[i] == "'":
            break
        chars.append(tail[i])
        i += 1
    try:
        payload = json.loads("".join(chars))
    except json.JSONDecodeError:
        return []
    filters = []
    for table in payload.get("tables") or []:
        for item in table.get("filters") or []:
            filters.append({
                "table": table.get("name"),
                "name": item.get("name"),
                "expr": item.get("expr"),
                "description": item.get("description"),
                "synonyms": item.get("synonyms") or [],
            })
    return filters


def parse_ddl(text):
    view = {"tables": [], "relationships": [], "view_metrics": [], "warnings": [], "verified_queries": [], "named_filters": []}
    match = re.search(r"semantic\s+view\s+([\w.\"]+)", text, re.I)
    if not match:
        raise ValueError("No CREATE SEMANTIC VIEW statement found.")
    view["fqn"] = match.group(1).replace('"', "")
    view["name"] = view["fqn"].split(".")[-1]
    sections, tail = _sections(text)
    tables = {}
    for item in _split_top(sections.get("tables", "")):
        head, synonyms, comment = _cut_clauses(item)
        mm = re.match(r"(?:(\w+)\s+as\s+)?([\w.]+)(.*)$", head, re.I | re.S)
        if not mm:
            view["warnings"].append("unparsed table: " + head[:160])
            continue
        alias, fqn, rest = mm.group(1), mm.group(2), mm.group(3)
        base = [part.replace('"', "") for part in fqn.split(".")]
        alias = alias or base[-1]
        pk = re.search(r"primary\s+key\s*\(([^)]*)\)", rest, re.I)
        unique = [[c.strip() for c in group.split(",")] for group in re.findall(r"unique\s*\(([^)]*)\)", rest, re.I)]
        table = {
            "name": alias, "base": base,
            "pk": [c.strip() for c in pk.group(1).split(",")] if pk else [],
            "unique": unique, "description": comment, "synonyms": synonyms,
            "fields": [], "metrics": [],
        }
        view["tables"].append(table)
        tables[alias.upper()] = table
    for item in _split_top(sections.get("relationships", "")):
        head, _, _ = _cut_clauses(item)
        mm = re.match(
            r"(?:(\w+)\s+as\s+)?(\w+)\s*\(([^)]*)\)\s+references\s+(\w+)(?:\s*\(([^)]*)\))?",
            head, re.I | re.S,
        )
        if not mm:
            view["warnings"].append("unparsed relationship: " + head[:160])
            continue
        view["relationships"].append({
            "name": mm.group(1) or f"{mm.group(2)}_TO_{mm.group(4)}",
            "table": mm.group(2),
            "columns": [c.strip() for c in mm.group(3).split(",")],
            "ref_table": mm.group(4),
            "ref_columns": [c.strip() for c in (mm.group(5) or "").split(",") if c.strip()],
        })
    for kw, kind in (("facts", "fact"), ("dimensions", "dimension"), ("metrics", "metric")):
        for item in _split_top(sections.get(kw, "")):
            head, synonyms, comment = _cut_clauses(item)
            private = bool(re.match(r"private\s+", head, re.I))
            head = re.sub(r"^(private|public)\s+", "", head, flags=re.I)
            mm = re.match(
                r"(?:(\w+)\.)?(\w+)\s+((?:(?:labels\s*=\s*\([^)]*\)|non\s+additive\s+by\s*\([^)]*\)|using\s*\([^)]*\))\s*)*)as\s+(.*)$",
                head, re.I | re.S,
            )
            if not mm:
                view["warnings"].append(f"unparsed {kw}: " + head[:160])
                continue
            tname, name, mods, expr = mm.group(1), mm.group(2), (mm.group(3) or "").strip(), mm.group(4).strip()
            extra = mods or None
            em = re.search(r"\s+(non\s+additive\s+by\s*\(.*\)|using\s*\(.*\))\s*$", expr, re.I | re.S)
            if em:
                extra = ((extra + " ") if extra else "") + em.group(1)
                expr = expr[: em.start()].strip()
            field = {
                "name": name, "expr": expr, "synonyms": synonyms,
                "description": comment, "private": private, "extra": extra,
            }
            if not tname or tname.upper() not in tables:
                if kind == "metric":
                    view["view_metrics"].append(field)
                else:
                    view["warnings"].append(f"{kind} {name} has no table")
                continue
            if kind == "metric":
                tables[tname.upper()]["metrics"].append(field)
            else:
                field["kind"] = kind
                tables[tname.upper()]["fields"].append(field)
    cm = re.search(r"(?<![\w.])comment\s*=\s*'((?:[^']|'')*)'", tail, re.I)
    view["description"] = cm.group(1).replace("''", "'") if cm else None
    im = re.search(r"ai_sql_generation\s+'((?:[^']|'')*)'", tail, re.I)
    view["instructions"] = im.group(1).replace("''", "'") if im else None
    view["verified_queries"] = _verified_queries(text)
    view["named_filters"] = _named_filters(tail)
    return view


def _using_extra(metric):
    names = []
    for rel in metric.get("using_relationships") or []:
        if isinstance(rel, str) and rel.strip():
            names.append(rel.strip())
        elif isinstance(rel, dict) and rel.get("name"):
            names.append(str(rel["name"]))
    if not names:
        return None
    return "using (" + ", ".join(names) + ")"


def parse_yaml(text):
    if "$$" in text:
        text = text.split("$$", 2)[1]
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict) or "name" not in doc:
        raise ValueError("YAML input needs a top-level name.")
    view = {
        "name": doc["name"], "fqn": doc["name"], "description": doc.get("description"),
        "instructions": (doc.get("module_custom_instructions") or {}).get("sql_generation"),
        "tables": [], "relationships": [], "view_metrics": [],
        "warnings": [], "verified_queries": [], "named_filters": [],
    }
    for metric in doc.get("metrics") or []:
        view["view_metrics"].append({
            "name": metric.get("name"), "expr": metric.get("expr") or "",
            "synonyms": metric.get("synonyms") or [], "description": metric.get("description"),
            "private": False, "extra": _using_extra(metric),
        })
    for table in doc.get("tables") or []:
        base = table.get("base_table") or {}
        parsed = {
            "name": table["name"],
            "base": [base.get("database"), base.get("schema"), base.get("table")],
            "pk": (table.get("primary_key") or {}).get("columns") or [],
            "unique": [], "description": table.get("description"),
            "synonyms": table.get("synonyms") or [], "fields": [], "metrics": [],
        }
        parsed["base"] = [part for part in parsed["base"] if part]
        for kind, key in (("fact", "facts"), ("dimension", "dimensions"), ("dimension", "time_dimensions")):
            for field in table.get(key) or []:
                parsed["fields"].append({
                    "name": field["name"], "expr": field.get("expr") or field["name"], "kind": kind,
                    "synonyms": field.get("synonyms") or [], "description": field.get("description"),
                    "private": field.get("access_modifier") == "private_access", "extra": None,
                    "data_type": field.get("data_type"),
                })
        for metric in table.get("metrics") or []:
            parsed["metrics"].append({
                "name": metric["name"], "expr": metric.get("expr") or "",
                "synonyms": metric.get("synonyms") or [], "description": metric.get("description"),
                "private": metric.get("access_modifier") == "private_access", "extra": _using_extra(metric),
            })
        view["tables"].append(parsed)
    for rel in doc.get("relationships") or []:
        cols = rel.get("relationship_columns") or []
        view["relationships"].append({
            "name": rel["name"], "table": rel.get("left_table"), "ref_table": rel.get("right_table"),
            "columns": [c.get("left_column") for c in cols],
            "ref_columns": [c.get("right_column") for c in cols],
        })
    for query in doc.get("verified_queries") or []:
        view["verified_queries"].append({
            "name": query.get("name"), "question": query.get("question"),
            "sql": query.get("sql"), "onboarding": bool(query.get("onboarding")),
        })
    return view


def parse_text(text):
    if not text or not text.strip():
        raise ValueError("Paste a semantic view first.")
    if re.search(r"create\s+(or\s+replace\s+)?semantic\s+view", text, re.I):
        return parse_ddl(text)
    return parse_yaml(text)
