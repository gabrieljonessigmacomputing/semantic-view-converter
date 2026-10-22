"""Compare this converter with the base semantic-tools embed.

The embed is the Snowflake Semantic View engine behind
https://twells89.github.io/sigma-data-model-manager/. It runs offline.
This script does not read API credentials.

    python3 tests/compare_to_base.py
"""

import json
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from converter.build import _catalog_index, _columns_for, _norm, convert  # noqa: E402

ZYWAVE = pathlib.Path("/Users/gabrielegjones/.cursor/zywave/sv-to-sigma")
DDL = ZYWAVE / "work" / "zip_fixed"
BUILD = ZYWAVE / "work" / "build"
EMBED_CANDIDATES = [
    os.environ.get("SEMANTIC_TOOLS_EMBED", ""),
    "/tmp/sv-dmm-probe/mcp/vendor/semantic-tools/semantic-tools-embed.mjs",
]
WAREHOUSE = re.compile(r"\[([^\]/]+)/([^\]]+)\]")
BARE = re.compile(r"\[([^\]/]+)\]")


def embed_path():
    for candidate in EMBED_CANDIDATES:
        if candidate and pathlib.Path(candidate).is_file():
            return candidate
    return None


def elements(spec):
    if not spec:
        return []
    return [el for page in spec.get("pages") or [] for el in page.get("elements") or []]


def base_convert_all(views, embed):
    runner = f"""
import {{ readFileSync }} from 'node:fs';
import {{ convertSemanticView }} from {json.dumps(embed)};
const views = JSON.parse(readFileSync(0, 'utf8'));
const out = [];
for (const view of views) {{
  const result = convertSemanticView(view.sql, {{
    connectionId: 'REPLACE_ME',
    bestEffort: true,
    synonymsInDescription: true,
    crossTableReferences: 'engine',
    usingPaths: 'engine',
  }});
  const first = (result.results || [])[0] || {{}};
  const codes = {{}};
  for (const finding of first.findings || []) {{
    codes[finding.code] = (codes[finding.code] || 0) + 1;
  }}
  out.push({{
    name: view.name,
    ok: Boolean(first.ok),
    spec: first.spec ? JSON.parse(first.spec) : null,
    codes,
  }});
}}
process.stdout.write(JSON.stringify(out));
"""
    payload = json.dumps([{"name": name, "sql": sql} for name, sql in views])
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", runner],
        input=payload, text=True, capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-800:])
    return json.loads(proc.stdout)


def classify_base(spec, catalog_index):
    """Count the base engine's known failure modes on one spec."""
    els = elements(spec)
    ids = {col["id"] for el in els for col in el.get("columns") or []}
    title_case = 0
    physical = 0
    calculated = 0
    phantom_formula = 0
    unnamed = 0
    broken = 0
    semi = 0
    bad_rel = 0
    for el in els:
        if not el.get("name"):
            unnamed += 1
        path = ((el.get("source") or {}).get("path")) or []
        known = _columns_for(path, catalog_index) if path else {}
        names = set()
        for col in el.get("columns") or []:
            if not col.get("name"):
                unnamed += 1
            else:
                names.add(col["name"])
            match = WAREHOUSE.fullmatch(col.get("formula") or "")
            if not match:
                calculated += 1
                continue
            part = match.group(2)
            hit = known.get(_norm(part))
            if hit and hit[0] == part:
                physical += 1
            elif hit:
                title_case += 1
            elif any(c.islower() for c in part):
                phantom_formula += 1
            else:
                physical += 1
        for metric in el.get("metrics") or []:
            formula = metric.get("formula") or ""
            if "If(" in formula and ("Max(" in formula or "Min(" in formula):
                semi += 1
            for ref in BARE.findall(formula):
                if ref not in names:
                    broken += 1
        for rel in el.get("relationships") or []:
            for key in rel.get("keys") or []:
                if key.get("sourceColumnId") not in ids or key.get("targetColumnId") not in ids:
                    bad_rel += 1
    return {
        "elements": len(els),
        "columns": sum(len(el.get("columns") or []) for el in els),
        "metrics": sum(len(el.get("metrics") or []) for el in els),
        "title_case_formulas": title_case,
        "physical_formulas": physical,
        "calculated_formulas": calculated,
        "unlisted_mixed_case_formulas": phantom_formula,
        "unnamed": unnamed,
        "broken_metric_refs": broken,
        "semi_additive_formulas": semi,
        "relationships_missing_columns": bad_rel,
    }


def add(bucket, key, value):
    bucket[key] = bucket.get(key, 0) + value


def certified_check():
    """Semantic-view metrics (not draft fact metrics) against the shipped specs."""
    if not BUILD.is_dir():
        return
    catalog = json.loads((ZYWAVE / "work" / "catalog_types.json").read_text())
    matched = 0
    missing = 0
    different = []
    columns_matched = 0
    fact_note = 0
    for spec_path in sorted(BUILD.glob("*/*.spec.json")):
        name = spec_path.parent.name
        sql_path = DDL / f"{name}.sql"
        if not sql_path.exists():
            continue
        names_path = ZYWAVE / "names" / f"{name}.json"
        skip_path = ZYWAVE / "skip_facts" / f"{name}.json"
        result = convert(
            sql_path.read_text(),
            catalog=catalog,
            names=json.loads(names_path.read_text()) if names_path.exists() else None,
            skip_facts=json.loads(skip_path.read_text()) if skip_path.exists() else None,
            facts_as_metrics=False,
        )
        ours = {m["id"]: m for el in elements(result["spec"]) for m in el.get("metrics") or []}
        our_cols = {c["id"]: c for el in elements(result["spec"]) for c in el.get("columns") or []}
        certified = json.loads(spec_path.read_text())
        for el in elements(certified):
            for col in el.get("columns") or []:
                if WAREHOUSE.fullmatch(col.get("formula") or ""):
                    continue
                got = our_cols.get(col["id"])
                if not got:
                    different.append((name, col.get("name") or col["id"], "column missing", col["formula"], ""))
                    continue
                if got["formula"] != col["formula"]:
                    different.append((name, col.get("name") or col["id"], "column", col["formula"], got["formula"]))
                else:
                    columns_matched += 1
            for metric in el.get("metrics") or []:
                if "__m__f_" in metric["id"]:
                    fact_note += 1
                    continue
                got = ours.get(metric["id"])
                if not got:
                    missing += 1
                    different.append((name, metric["name"], "missing", metric["formula"], ""))
                    continue
                if got["formula"] == metric["formula"]:
                    matched += 1
                else:
                    different.append((name, metric["name"], "formula", metric["formula"], got["formula"]))
    metric_diffs = [row for row in different if row[2] in ("formula", "missing")]
    column_diffs = [row for row in different if row[2] not in ("formula", "missing")]
    print(f"\nCertified semantic-view metrics: {matched} formulas match, {missing} missing, {len(metric_diffs) - missing} differ.")
    print(f"Certified calculated columns: {columns_matched} match, {len(column_diffs)} differ.")
    print(f"Certified draft fact metrics (id contains __m__f_) were not compared: {fact_note}.")
    for name, metric, kind, certified_formula, ours_formula in different[:20]:
        print(f"  {name} / {metric} ({kind})")
        print(f"    certified: {certified_formula}")
        if ours_formula:
            print(f"    ours:      {ours_formula}")
    if len(different) > 20:
        print(f"  … {len(different) - 20} more")


def main():
    embed = embed_path()
    if not embed or not DDL.is_dir():
        print("Need the semantic-tools embed and the local DDL folder to compare.")
        return 1
    header = pathlib.Path(embed).read_text(errors="ignore")[:400]
    print(f"Base engine file: {embed}")
    version = re.search(r"semantic-tools-embed@([0-9.]+)", header)
    if version:
        print(f"Embed version: {version.group(1)}")
    views = [(path.stem, path.read_text()) for path in sorted(DDL.glob("*.sql"))]
    catalog = json.loads((ZYWAVE / "work" / "catalog_types.json").read_text())
    catalog_index = _catalog_index(catalog)
    base_rows = base_convert_all(views, embed)
    base_t, ours_t = {}, {}
    base_codes = {}
    ai_base = ai_ours = vq_ours = 0
    for (name, sql), base in zip(views, base_rows):
        ours = convert(sql, catalog=catalog, facts_as_metrics=False)
        b = classify_base(base["spec"], catalog_index)
        o = classify_base(ours["spec"], catalog_index)
        for key, value in b.items():
            add(base_t, key, value)
        for key, value in o.items():
            add(ours_t, key, value)
        for key, value in base["codes"].items():
            add(base_codes, key, value)
        if ours["aiContext"] and "Instructions from the semantic view" in ours["aiContext"]:
            ai_ours += 1
        if ours["verifiedQueries"]:
            vq_ours += 1
        base_blob = json.dumps(base["spec"])
        if "ai_sql" in base_blob or "Instructions from the semantic view" in base_blob:
            ai_base += 1
        print(
            f"{name:38} base title={b['title_case_formulas']:4} broken={b['broken_metric_refs']:3} "
            f"unnamed={b['unnamed']:4} bad_rel={b['relationships_missing_columns']:2} metrics={b['metrics']:3} | "
            f"ours title={o['title_case_formulas']:3} broken={o['broken_metric_refs']:3} "
            f"semi={o['semi_additive_formulas']:2} metrics={o['metrics']:3} errors="
            f"{sum(1 for f in ours['findings'] if f['level'] == 'error')}"
        )
    print("\nTOTAL base", json.dumps(base_t, sort_keys=True))
    print("TOTAL ours", json.dumps(ours_t, sort_keys=True))
    print("BASE finding codes", json.dumps(base_codes, sort_keys=True))
    print(f"AI instructions present in the model JSON: base {ai_base}, ours sidecar {ai_ours}")
    print(f"Views with verified questions kept by ours: {vq_ours}")
    certified_check()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
