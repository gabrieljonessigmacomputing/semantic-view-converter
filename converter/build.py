"""Build a Sigma data-model spec from a parsed semantic view.

Nothing here calls Sigma or Snowflake. A connection id, when the caller supplies
one, is written into the spec as an opaque id. It is never a client secret.
"""

import re

from .names import also_called, column_name, humanize, metric_name, slug
from .parse import parse_text
from .translate import TranslateError, translate

CURRENCY = {"kind": "number", "formatString": "$,.2f"}
PLAIN = {"kind": "number", "formatString": ",.2f"}
INTEGER = {"kind": "number", "formatString": ",.0f"}
DATE = {"kind": "datetime", "formatString": "%b %d, %Y"}

NON_ADDITIVE = re.compile(
    r"PRICE|RATE\b|_RATE|RATIO|PERCENT|PCT|SCORE|\bAVG|AVG_|AVERAGE|PROBABILITY|"
    r"LATITUDE|LONGITUDE|DAYS|_AGE\b|SENTIMENT|NPS|RATING|RANK|LIKELIHOOD|WEIGHT|ALLOCATION_PERCENTAGE",
    re.I,
)
NON_ADDITIVE_TEXT = re.compile(
    r"\b(score|scale|rate|ratio|percent|percentage|average|avg|days|likelihood|probability|rating|index)\b|1-5|0-100|0-1\b",
    re.I,
)
NOT_MONEY = re.compile(
    r"QUANTITY|QTY|COUNT|NUMBER|NUM_|UNITS|HOURS|MINUTES|SECONDS|DURATION|DAYS|PERCENT|PCT|"
    r"RATE|RATIO|SCORE|SEATS|USERS|LICENSES|EVENTS|SESSIONS|_ID\b|YEAR|MONTH",
    re.I,
)
MONEY = re.compile(
    r"amount|price|revenue|balance|cost|tax|payment|credit|debit|refund|\$|\barr\b|_arr|arr_|mrr|"
    r"\bfee|charge|spend|bookings|acv|tcv|dollar|usd|subtotal|total",
    re.I,
)
COL_REF = re.compile(r"\[([^\]/]+)\]")
PLACEHOLDER_CONNECTION = "REPLACE_ME"


def _norm(text):
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def _bare_physical(expr, table_name):
    match = re.fullmatch(r'(?:(\w+)\.)?(?:"([^"]+)"|([A-Za-z_][\w$]*))', expr.strip())
    if not match:
        return None
    qual, quoted, bare = match.group(1), match.group(2), match.group(3)
    if qual and qual.upper() != table_name.upper():
        return None
    return quoted if quoted is not None else bare.upper()


def _catalog_index(catalog):
    """{FULL.PATH and TABLE: {norm(column): (spelling, type or None)}}"""
    index = {}
    if not catalog:
        return index
    if not isinstance(catalog, dict):
        raise ValueError("Column catalog must be a JSON object of table → columns.")
    for key, value in catalog.items():
        cols = {}
        if isinstance(value, dict):
            for col, typ in value.items():
                cols[_norm(col)] = (col, str(typ).lower() if typ else None)
        elif isinstance(value, list):
            for col in value:
                cols[_norm(col)] = (str(col), None)
        else:
            raise ValueError(f"Catalog entry {key} must be a column map or a list of column names.")
        index[str(key).upper()] = cols
    return index


def _columns_for(base, index):
    if not index:
        return {}
    full = ".".join(base).upper()
    if full in index:
        return index[full]
    table = base[-1].upper()
    hits = [cols for key, cols in index.items() if key == table or key.endswith("." + table)]
    if len(hits) == 1:
        return hits[0]
    return {}


def _type_kind(typ):
    if not typ:
        return None
    t = typ.lower()
    if any(word in t for word in ("timestamp", "datetime", "date")):
        return "datetime"
    if any(word in t for word in ("int", "number", "decimal", "numeric", "float", "double", "real")):
        if re.search(r"\(\s*\d+\s*,\s*0\s*\)", t):
            return "integer"
        if "int" in t and "interval" not in t and "decimal" not in t and "number" not in t:
            return "integer"
        return "number"
    if "bool" in t:
        return "boolean"
    return "text"


def _format_for(logical_name, synonyms, description, type_kind):
    if type_kind == "datetime":
        return DATE
    if type_kind not in ("number", "integer"):
        return None
    text = " ".join([logical_name, *(synonyms or []), description or ""])
    if type_kind == "number" and MONEY.search(text) and not NOT_MONEY.search(logical_name):
        return CURRENCY
    if type_kind == "integer":
        return INTEGER
    return PLAIN


def _semi(extra):
    if not extra:
        return None
    match = re.search(
        r"non\s+additive\s+by\s*\(\s*(?:(\w+)\.)?(\w+)\s+(asc|desc)",
        extra, re.I,
    )
    if not match:
        return None
    return {"qualifier": match.group(1), "name": match.group(2), "direction": match.group(3).lower()}


def _is_filter(extra):
    return bool(extra) and re.search(r"labels\s*=\s*\(\s*filter\s*\)", extra, re.I)


def _wrap_semi(formula, dim_name, direction):
    extreme = "Min" if direction == "desc" else "Max"

    def repl(match):
        name = match.group(1)
        if name == dim_name:
            return match.group(0)
        return f"If([{dim_name}] = {extreme}([{dim_name}]), [{name}])"

    return COL_REF.sub(repl, formula)


def _unique_name(base, used):
    if base.lower() not in used:
        used.add(base.lower())
        return base, None
    alt = f"{base} 2"
    n = 2
    while alt.lower() in used:
        n += 1
        alt = f"{base} {n}"
    used.add(alt.lower())
    return alt, f"Renamed {base} to {alt} because the name was already used."


def _skip_rule(skip_facts, table_name, field_name):
    if not skip_facts:
        return None
    raw = None
    if isinstance(skip_facts, dict):
        for key, value in skip_facts.items():
            if key.upper() == table_name.upper():
                raw = value
                break
    if raw is None:
        return None
    if isinstance(raw, list):
        return "skip" if any(str(item).upper() == field_name.upper() for item in raw) else None
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key.upper() == field_name.upper():
                return value
    return None


def _agg_choice(field, rule):
    if isinstance(rule, dict):
        return rule.get("agg") or "Sum", rule.get("only_one")
    if isinstance(rule, str) and rule.lower() != "skip":
        return rule[:1].upper() + rule[1:].lower() if rule.lower() in ("sum", "avg", "min", "max") else rule, None
    text = " ".join([field["name"], *(field.get("synonyms") or []), field.get("description") or ""])
    if NON_ADDITIVE.search(field["name"]) or NON_ADDITIVE_TEXT.search(text):
        return "Avg", None
    return "Sum", None


def _ai_context(view, elements):
    lines = [
        f"# {view['name']}",
        "",
        "Paste everything below the line into the data model's AI context. "
        "Sigma's API cannot set that field.",
        "",
        "---",
        "",
    ]
    if view.get("description"):
        lines += [view["description"].strip(), ""]
    if view.get("instructions"):
        lines += ["## Instructions from the semantic view", "", view["instructions"].strip(), ""]
    lines += ["## Tables", ""]
    for element in elements:
        pk = element.get("_pk") or []
        grain = "one row per " + ", ".join(pk) if pk else "grain not declared"
        lines.append(f"- {element['name']}: {grain}.")
    if view.get("named_filters"):
        lines += ["", "## Named filters", "",
                  "These are not applied for you. A default filter belongs in an `In Default Scope (...)` column after review.",
                  ""]
        for item in view["named_filters"]:
            lines.append(f"- {item.get('table')}.{item.get('name')}: {item.get('expr')}")
    if view.get("verified_queries"):
        lines += ["", "## Verified questions", ""]
        for query in view["verified_queries"]:
            lines.append(f"- {query['question']}")
    lines += [
        "",
        "Metrics are not pre-filtered. Where the semantic view says 'by default', filter the matching flag column to TRUE.",
        "",
    ]
    return "\n".join(lines)


def _qualify(base, database, schema):
    """Fill in a database and schema only when the view left the table path short."""
    parts = [part for part in base if part]
    if len(parts) >= 3:
        return parts, None
    if len(parts) == 2 and database:
        return [database, *parts], None
    if len(parts) == 1 and database and schema:
        return [database, schema, *parts], None
    needs = {}
    if not database:
        needs["database"] = [parts[-1] if parts else ""]
    if len(parts) < 2 and not schema:
        needs["schema"] = [parts[-1] if parts else ""]
    return parts, needs


def convert(source, connection_id=None, catalog=None, facts_as_metrics=True, names=None, skip_facts=None,
            synonyms_in_description=True, infer_number_formats=True, database=None, schema=None):
    view = parse_text(source)
    findings = []
    for warning in view["warnings"]:
        findings.append({"level": "error", "code": "PARSE", "message": warning})

    needs = {"database": [], "schema": []}
    for table in view["tables"]:
        qualified, missing = _qualify(table["base"], (database or "").strip(), (schema or "").strip())
        table["base"] = qualified
        if missing:
            for key, vals in missing.items():
                needs[key].extend(vals)
    if needs["database"] or needs["schema"]:
        return {
            "ok": False,
            "viewName": view["name"],
            "spec": None,
            "needs": {key: vals for key, vals in needs.items() if vals},
            "findings": findings,
            "aiContext": "",
            "verifiedQueries": [],
            "namedFilters": [],
            "counts": {},
        }

    conn = (connection_id or "").strip() or PLACEHOLDER_CONNECTION
    if conn == PLACEHOLDER_CONNECTION:
        findings.append({
            "level": "info", "code": "CONNECTION",
            "message": "No connection id was supplied. The spec uses REPLACE_ME. Paste your Sigma connection id before saving the model.",
        })

    catalog_index = _catalog_index(catalog)
    names = names or {}
    by_table = {table["name"].upper(): table for table in view["tables"]}

    # Pass 1: columns, so metric formulas can name them.
    elements = []
    for table in view["tables"]:
        element_id = slug(table["name"])
        base_name = (names.get(table["name"]) or names.get(table["name"].upper()) or humanize(table["name"]))
        used_names = set()
        display, renamed = _unique_name(base_name, set())
        if renamed:
            findings.append({"level": "info", "code": "RENAME", "message": renamed})
        description = (table.get("description") or "").strip()
        description = (description + (also_called(table.get("synonyms")) if synonyms_in_description else "")).strip()
        catalog_cols = _columns_for(table["base"], catalog_index)
        if catalog and not catalog_cols:
            findings.append({
                "level": "warning", "code": "CATALOG_MISS",
                "message": f"No catalog entry for {'.'.join(table['base'])}. Physical names are taken from the DDL.",
            })

        columns = []
        by_logical = {}
        by_physical = {}
        for field in table["fields"]:
            physical = _bare_physical(field["expr"], table["name"])
            col_id = f"{element_id}__{slug(physical or field['name'])}"
            if any(col["id"] == col_id for col in columns):
                col_id = f"{col_id}_2"
            preferred = None
            col_overrides = names.get("__columns__") or {}
            for key, value in col_overrides.items():
                if key.upper() == f"{table['name']}.{field['name']}".upper():
                    preferred = value
            base = preferred or column_name(field["name"], field.get("synonyms"))
            col_display, renamed = _unique_name(base, used_names)
            if renamed:
                findings.append({"level": "info", "code": "RENAME", "message": f"{table['name']}: {renamed}"})
            record = {
                "id": col_id,
                "logical": field["name"],
                "physical": physical,
                "display": col_display,
                "field": field,
            }
            by_logical[field["name"].upper()] = record
            if physical:
                by_physical[_norm(physical)] = record
            columns.append(record)
        elements.append({
            "id": element_id,
            "name": display,
            "table": table,
            "description": description,
            "catalog": catalog_cols,
            "columns": columns,
            "by_logical": by_logical,
            "by_physical": by_physical,
            "_pk": table.get("pk") or [],
            "metrics": [],
            "used_names": used_names,
        })

    by_element = {el["table"]["name"].upper(): el for el in elements}

    def warehouse_ref(element, spelling):
        catalog_cols = element["catalog"]
        hit = catalog_cols.get(_norm(spelling)) if catalog_cols else None
        if hit:
            physical = hit[0]
            type_kind = _type_kind(hit[1])
        else:
            physical = spelling
            type_kind = None
            if catalog_cols:
                findings.append({
                    "level": "error", "code": "UNRESOLVED_COLUMN",
                    "message": f"{element['name']}: {spelling} is not in the column catalog for {'.'.join(element['table']['base'])}.",
                })
        table_name = element["table"]["base"][-1]
        return f"[{table_name}/{physical}]", type_kind

    def field_on(element, ident):
        return element["by_logical"].get(ident.upper()) or element["by_physical"].get(_norm(ident))

    def ensure_helper(element, physical_name, reason="metric"):
        found = field_on(element, physical_name)
        if found:
            return found
        # Unquoted Snowflake identifiers are case-insensitive and stored uppercase.
        # A quoted identifier keeps its spelling, including spaces.
        if re.fullmatch(r"[A-Za-z_][\w$]*", physical_name):
            spelling = physical_name.upper()
        else:
            spelling = physical_name
        formula, type_kind = warehouse_ref(element, spelling)
        display, renamed = _unique_name(humanize(spelling), element["used_names"])
        if renamed:
            findings.append({"level": "info", "code": "RENAME", "message": renamed})
        col_id = f"{element['id']}__{slug(spelling)}"
        if any(col["id"] == col_id for col in element["columns"]):
            col_id = f"{col_id}_2"
        record = {
            "id": col_id, "logical": spelling, "physical": spelling, "display": display,
            "field": {"name": spelling, "kind": "helper", "synonyms": [], "description": None, "expr": spelling},
            "type_kind": type_kind,
        }
        if reason == "key":
            desc = "Relationship key. The semantic view names this column and does not declare it as a field."
            why = "The relationship names it, and it is not a declared field."
        else:
            desc = "Helper column: used by the semantic view's own metrics but not declared as a field."
            why = "The semantic view's metrics use it, and it is not a declared field."
        spec_col = {"id": col_id, "name": display, "formula": formula, "description": desc}
        fmt = _format_for(spelling, [], "", type_kind) if infer_number_formats else None
        if fmt:
            spec_col["format"] = fmt
        record["spec"] = spec_col
        element["columns"].append(record)
        element["by_logical"][spelling] = record
        element["by_physical"][_norm(spelling)] = record
        findings.append({
            "level": "info", "code": "HELPER_COLUMN",
            "message": f"{element['name']}.{display} = {formula}. {why}",
        })
        return record

    def make_resolve(element, allow_physical):
        def resolve(parts):
            target = element
            ident = parts[-1]
            cross = False
            if len(parts) >= 2:
                head = parts[0].upper()
                if head != element["table"]["name"].upper():
                    other = by_element.get(head)
                    if other is None:
                        raise TranslateError("unknown reference " + ".".join(parts))
                    target = other
                    cross = True
            found = field_on(target, ident)
            if not found and (allow_physical or cross or len(parts) <= 2):
                found = ensure_helper(target, ident)
            if not found:
                raise TranslateError("unknown field " + ".".join(parts))
            if cross:
                return f"[{_rel_display(element, target)}/{found['display']}]"
            return f"[{found['display']}]"
        return resolve

    def _rel_display(src, dst):
        for rel in view["relationships"]:
            if rel["table"].upper() == src["table"]["name"].upper() and rel["ref_table"].upper() == dst["table"]["name"].upper():
                return humanize(rel["name"])
        return dst["name"]

    # Column formulas.
    for element in elements:
        for col in element["columns"]:
            field = col["field"]
            physical = col["physical"]
            if physical:
                formula, type_kind = warehouse_ref(element, physical)
            else:
                try:
                    formula, calls = translate(field["expr"], make_resolve(element, allow_physical=True))
                except TranslateError as exc:
                    formula = f'/* untranslated: {field["expr"][:80]} */'
                    findings.append({
                        "level": "error", "code": "UNTRANSLATED",
                        "message": f"{element['name']}.{field['name']}: {exc}",
                    })
                    calls = []
                    type_kind = None
                else:
                    type_kind = None
                for call in calls:
                    if call.startswith("WAREHOUSE:"):
                        findings.append({
                            "level": "warning", "code": "WAREHOUSE_FUNCTION",
                            "message": f"{element['name']}.{col['display']} calls {call.split(':', 1)[1]}. It runs only if the connection's role can see that function.",
                        })
            if field.get("private"):
                findings.append({
                    "level": "info", "code": "PRIVATE",
                    "message": f"{element['name']}.{col['display']} is PRIVATE on the semantic view. It is included because metrics may reference it.",
                })
            if type_kind is None and field.get("data_type"):
                type_kind = _type_kind(field.get("data_type"))
            fmt = _format_for(field["name"], field.get("synonyms"), field.get("description"), type_kind) if infer_number_formats else None
            desc = ((field.get("description") or "").strip() + (also_called(field.get("synonyms")) if synonyms_in_description else "")).strip()
            spec_col = {"id": col["id"], "name": col["display"], "formula": formula}
            if desc:
                spec_col["description"] = desc
            if fmt:
                spec_col["format"] = fmt
            col["spec"] = spec_col
            col["type_kind"] = type_kind if physical else None

    # Relationships. Keys are logical field names. Never invent a second column for a logical alias.
    rel_ids = set()
    for rel in view["relationships"]:
        src = by_element.get(rel["table"].upper())
        dst = by_element.get(rel["ref_table"].upper())
        if not src or not dst:
            findings.append({
                "level": "error", "code": "RELATIONSHIP",
                "message": f"{rel['name']} references a table that is not in the view.",
            })
            continue
        keys = []
        ok = True
        pairs = list(zip(rel["columns"], rel["ref_columns"] or []))
        if rel["ref_columns"] and len(rel["columns"]) != len(rel["ref_columns"]):
            findings.append({
                "level": "error", "code": "RELATIONSHIP",
                "message": f"{rel['name']} has {len(rel['columns'])} source columns and {len(rel['ref_columns'])} target columns.",
            })
            ok = False
        for source_name, target_name in pairs:
            source_col = field_on(src, source_name) or ensure_helper(src, source_name, reason="key")
            target_col = field_on(dst, target_name) or ensure_helper(dst, target_name, reason="key")
            if not source_col or not target_col:
                ok = False
                missing = source_name if not source_col else target_name
                findings.append({
                    "level": "error", "code": "RELATIONSHIP",
                    "message": f"{rel['name']} names {missing}, which is not a column.",
                })
                continue
            keys.append({"sourceColumnId": source_col["id"], "targetColumnId": target_col["id"]})
        if not ok:
            continue
        rel_id = f"{src['id']}__to__{dst['id']}"
        if rel_id in rel_ids:
            rel_id = f"{rel_id}__{slug(rel['name'])}"
        rel_ids.add(rel_id)
        src.setdefault("relationships", []).append({
            "id": rel_id,
            "name": humanize(rel["name"]),
            "targetElementId": dst["id"],
            "keys": keys,
            "relationshipType": "N:1",
        })

    # Metrics.
    for element in elements:
        table = element["table"]
        semi = None
        for metric in table["metrics"]:
            found = _semi(metric.get("extra"))
            if found:
                dim_field = field_on(element, found["name"])
                if not dim_field:
                    findings.append({
                        "level": "error", "code": "SEMI_ADDITIVE",
                        "message": f"{metric['name']} is non-additive by {found['name']}, which is not a column.",
                    })
                else:
                    semi = {"display": dim_field["display"], "direction": found["direction"]}
                    break
        seen_metric_names = set()
        for metric in table["metrics"]:
            if re.search(r"\busing\s*\(", metric.get("extra") or "", re.I):
                findings.append({
                    "level": "warning", "code": "USING",
                    "message": f"{element['name']}.{metric['name']} is pinned with USING. Sigma has no join-path selector, so the metric is translated without that pin.",
                })
            try:
                formula, calls = translate(metric["expr"], make_resolve(element, allow_physical=False))
            except TranslateError as exc:
                findings.append({
                    "level": "error", "code": "METRIC_DROPPED",
                    "message": f"{element['name']}.{metric['name']} was not translated: {exc}. Expression: {metric['expr'][:180]}",
                })
                continue
            semi_of_this = _semi(metric.get("extra"))
            if semi_of_this:
                dim_field = field_on(element, semi_of_this["name"])
                if dim_field:
                    formula = _wrap_semi(formula, dim_field["display"], semi_of_this["direction"])
                    which = "earliest" if semi_of_this["direction"] == "desc" else "latest"
                    findings.append({
                        "level": "info", "code": "SEMI_ADDITIVE",
                        "message": f"{element['name']}.{metric_name(metric['name'])} uses the {which} {dim_field['display']} in each group ({formula}).",
                    })
            for call in calls:
                if call.startswith("WAREHOUSE:"):
                    findings.append({
                        "level": "warning", "code": "WAREHOUSE_FUNCTION",
                        "message": f"{element['name']}.{metric['name']} calls {call.split(':', 1)[1]}. It runs only if the connection's role can see that function.",
                    })
            broken = [ref for ref in COL_REF.findall(formula) if ref not in {c["display"] for c in element["columns"]} and "/" not in ref]
            # Related-column refs are [Rel/Col] and are not matched by COL_REF (which excludes slashes).
            if broken:
                findings.append({
                    "level": "error", "code": "BROKEN_REF",
                    "message": f"{element['name']}.{metric['name']} references {broken}, which are not columns.",
                })
            display, renamed = _unique_name(metric_name(metric["name"]), seen_metric_names)
            if renamed:
                findings.append({"level": "info", "code": "RENAME", "message": renamed})
            # Column names occupy the same element namespace in Sigma workbooks, not the metric namespace.
            desc_parts = []
            if metric.get("description"):
                desc_parts.append(metric["description"].strip())
            desc_parts.append(f"Semantic view definition: {metric['expr']}")
            if semi_of_this and field_on(element, semi_of_this["name"]):
                dim = field_on(element, semi_of_this["name"])["display"]
                desc_parts.append(f"Semi-additive like the semantic view: uses only the latest {dim} within each group.")
            if synonyms_in_description and metric.get("synonyms"):
                desc_parts.append("Also called: " + ", ".join(metric["synonyms"]) + ".")
            element["metrics"].append({
                "id": f"{element['id']}__m__{slug(metric['name'])}",
                "name": display,
                "formula": formula,
                "description": " ".join(part for part in desc_parts if part),
            })

        if facts_as_metrics:
            covered = set()
            for metric in table["metrics"]:
                for field in table["fields"]:
                    if re.search(r"\b" + re.escape(field["name"]) + r"\b", metric["expr"], re.I):
                        covered.add(field["name"].upper())
            for col in element["columns"]:
                field = col["field"]
                if field.get("kind") != "fact":
                    continue
                label = f"{element['name']}.{col['display']}"
                if _is_filter(field.get("extra")):
                    findings.append({"level": "info", "code": "FACT_SKIPPED", "message": f"{label} is a named filter, so it stays a column."})
                    continue
                if col["type_kind"] in ("text", "datetime", "boolean"):
                    findings.append({"level": "info", "code": "FACT_SKIPPED", "message": f"{label} is {col['type_kind']}, so it has no metric."})
                    continue
                if not col["physical"] and col["type_kind"] is None:
                    findings.append({"level": "info", "code": "FACT_SKIPPED", "message": f"{label} is a calculated fact. It stays a column."})
                    continue
                if field["name"].upper() in covered:
                    findings.append({"level": "info", "code": "FACT_SKIPPED", "message": f"{label} is already aggregated by a semantic-view metric."})
                    continue
                rule = _skip_rule(skip_facts, table["name"], field["name"])
                if rule == "skip" or (isinstance(rule, str) and rule.lower() == "skip"):
                    findings.append({"level": "info", "code": "FACT_SKIPPED", "message": f"{label} is skipped by the review file."})
                    continue
                agg, only_one = _agg_choice(field, rule)
                agg = {"sum": "Sum", "avg": "Avg", "min": "Min", "max": "Max"}.get(str(agg).lower(), agg)
                prefix = {"Avg": "Avg", "Max": "Max", "Min": "Min"}.get(agg)
                base = col["display"]
                if prefix and not base.lower().startswith(prefix.lower()):
                    base = f"{prefix} {base}"
                display, _ = _unique_name(base, seen_metric_names)
                ref = f"[{col['display']}]"
                if semi:
                    ref = f"If([{semi['display']}] = {'Min' if semi['direction'] == 'desc' else 'Max'}([{semi['display']}]), {ref})"
                formula = f"{agg}({ref})"
                guard_note = ""
                if only_one:
                    guard = field_on(element, only_one)
                    if not guard:
                        findings.append({
                            "level": "error", "code": "FACT_METRIC",
                            "message": f"{label} is guarded by {only_one}, which is not a column.",
                        })
                        continue
                    formula = f"If(CountDistinct([{guard['display']}]) = 1, {formula})"
                    guard_note = f" Blank unless exactly one {guard['display']} is in scope."
                why = "review file" if rule else "draft guess from the name (Sum, or Avg for rates, scores and days)"
                findings.append({
                    "level": "info", "code": "FACT_METRIC",
                    "message": f"{label} → {display} = {formula}. Aggregation: {why}.",
                })
                element["metrics"].append({
                    "id": f"{element['id']}__m__f_{slug(display)}",
                    "name": display,
                    "formula": formula,
                    "description": (
                        f"{agg} of the semantic view fact {table['name']}.{field['name']}.{guard_note} "
                        + (field.get("description") or "")
                    ).strip(),
                    "format": col["spec"].get("format"),
                })

    if view["view_metrics"]:
        for metric in view["view_metrics"]:
            findings.append({
                "level": "warning", "code": "VIEW_METRIC",
                "message": f"View-level metric {metric.get('name')} has no single table to live on, so it was not built.",
            })

    if view.get("instructions"):
        findings.append({
            "level": "info", "code": "AI_CONTEXT",
            "message": "The semantic view's AI instructions are in the AI context download. They are not hidden filters on the metrics.",
        })
    if view.get("verified_queries"):
        findings.append({
            "level": "info", "code": "VERIFIED_QUERIES",
            "message": (
                f"{len(view['verified_queries'])} verified "
                f"{'question is' if len(view['verified_queries']) == 1 else 'questions are'} "
                "in the download. They are acceptance tests, not model objects."
            ),
        })
    if view.get("named_filters"):
        findings.append({
            "level": "info", "code": "NAMED_FILTERS",
            "message": f"{len(view['named_filters'])} named filters were found. Default filters become In Default Scope columns only after someone confirms them.",
        })

    pages_elements = []
    for element in elements:
        spec = {
            "id": element["id"],
            "kind": "table",
            "name": element["name"],
            "source": {
                "kind": "warehouse-table",
                "connectionId": conn,
                "path": element["table"]["base"],
            },
            "columns": [col["spec"] for col in element["columns"]],
        }
        if element["description"]:
            spec["description"] = element["description"]
        if element["metrics"]:
            metrics_out = []
            for metric in element["metrics"]:
                item = {"id": metric["id"], "name": metric["name"], "formula": metric["formula"]}
                if metric.get("description"):
                    item["description"] = metric["description"]
                if metric.get("format"):
                    item["format"] = metric["format"]
                metrics_out.append(item)
            spec["metrics"] = metrics_out
        if element.get("relationships"):
            spec["relationships"] = element["relationships"]
        if spec["columns"]:
            spec["order"] = [col["id"] for col in spec["columns"]]
        pages_elements.append(spec)

    n_tables = len(pages_elements)
    grain = []
    for element in elements:
        if element["_pk"]:
            grain.append(f"{element['name']}: one row per {', '.join(element['_pk'])}.")
    description = (
        f"Sigma data model for the Snowflake semantic view {view['fqn']}: "
        f"{n_tables} table{'s' if n_tables != 1 else ''}, "
        f"{sum(len(el['columns']) for el in elements)} columns, "
        f"{len(view['relationships'])} relationship{'' if len(view['relationships']) == 1 else 's'}. "
        "Semantic-view metrics are translated as written. "
        "Facts are columns"
        + (" and, where noted, draft metrics." if facts_as_metrics else ".")
    )
    if view.get("description"):
        description += "\n\n" + view["description"].strip()
    if grain:
        description += "\n\nGrain: " + " ".join(grain)
    description += (
        "\n\nMetrics are not pre-filtered. AI instructions are not stored on the model "
        "(Sigma has no API for that); they are in the AI context file."
    )

    spec = {
        "name": view["name"],
        "description": description,
        "schemaVersion": 1,
        "kind": "data-model",
        "pages": [{"id": "page1", "name": "Page 1", "elements": pages_elements}],
    }
    errors = [f for f in findings if f["level"] == "error"]
    return {
        "ok": not errors and not view["warnings"],
        "viewName": view["name"],
        "spec": spec,
        "findings": findings,
        "aiContext": _ai_context(view, elements),
        "verifiedQueries": view.get("verified_queries") or [],
        "namedFilters": view.get("named_filters") or [],
        "counts": {
            "tables": n_tables,
            "columns": sum(len(el["columns"]) for el in elements),
            "metrics": sum(len(el["metrics"]) for el in elements),
            "relationships": len(view["relationships"]),
            "semanticViewMetrics": sum(len(t["metrics"]) for t in view["tables"]),
        },
    }
