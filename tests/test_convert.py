"""Converter behavior that the base semantic-tools engine gets wrong."""

import json
import pathlib
import re
import unittest

from converter.build import _catalog_index, _columns_for, _norm, convert

ROOT = pathlib.Path(__file__).resolve().parents[1]
ZYWAVE = pathlib.Path("/Users/gabrielegjones/.cursor/zywave/sv-to-sigma")

REPRO = """
create or replace semantic view DEMO.ANALYTICS.SV_ORDERS
	tables (
		ORDERS as DEMO.ANALYTICS.ORDERS primary key (ORDER_ID) with synonyms=('sales orders') comment='One row per order',
		CUSTOMERS as DEMO.ANALYTICS.CUSTOMERS primary key (CUSTOMER_KEY) comment='Customers'
	)
	relationships (
		ORDERS_TO_CUSTOMERS as ORDERS(CUSTOMER_ID) references CUSTOMERS(CUSTOMER_KEY)
	)
	facts (
		ORDERS.ORDER_AMOUNT as ORDER_TOTAL with synonyms=('revenue') comment='Order total',
		ORDERS.DAYS_TO_SHIP as DAYS_TO_SHIP comment='Days from order to ship'
	)
	dimensions (
		ORDERS.ORDER_ID as ORDER_ID comment='Order id',
		ORDERS.CUSTOMER_ID as CUSTOMER_ID comment='Customer id',
		ORDERS.ORDER_DATE as ORDERED_ON with synonyms=('as of date') comment='Order date',
		CUSTOMERS.CUSTOMER_KEY as ID comment='Customer key, physical column is ID',
		CUSTOMERS.CUSTOMER_NAME as NAME with synonyms=('customer') comment='Customer name'
	)
	metrics (
		ORDERS.TOTAL_REVENUE non additive by (ORDERS.ORDER_DATE asc nulls last) as SUM(ORDERS.ORDER_AMOUNT) comment='Latest-day revenue',
		ORDERS.STILL_OPEN as COUNT_IF(ORDERS.ORDER_DATE < CURRENT_TIMESTAMP()) comment='Uses the clock'
	)
	comment='Demo semantic view'
	ai_sql_generation 'By default, include only orders with an amount.'
	ai_verified_queries (
		TOP_CUSTOMERS AS ( QUESTION 'Who are the top customers?' ONBOARDING_QUESTION false SQL 'SELECT NAME, SUM(ORDER_TOTAL) AS REVENUE FROM DEMO.ANALYTICS.ORDERS' )
	);
"""


def elements(spec):
    return [el for page in spec["pages"] for el in page["elements"]]


class ConvertTests(unittest.TestCase):
    def test_repro_fixes_the_base_converter_failures(self):
        result = convert(REPRO, facts_as_metrics=False)
        self.assertTrue(result["ok"], result["findings"])
        els = {el["id"]: el for el in elements(result["spec"])}
        customers = els["customers"]
        orders = els["orders"]
        by_id = {col["id"]: col for col in customers["columns"]}
        self.assertEqual(by_id["customers__id"]["formula"], "[CUSTOMERS/ID]")
        self.assertNotIn("customers__customer_key", by_id)
        self.assertEqual(customers["name"], "Customers")
        self.assertEqual(orders["name"], "Orders")
        self.assertIn("Also called: sales orders.", orders["description"])
        rel = orders["relationships"][0]
        self.assertEqual(rel["keys"][0]["targetColumnId"], "customers__id")
        revenue = next(m for m in orders["metrics"] if m["name"] == "Total Revenue")
        self.assertEqual(revenue["formula"], 'Sum(If([As of Date] = Max([As of Date]), [Revenue]))')
        still_open = next(m for m in orders["metrics"] if m["name"] == "Still Open")
        self.assertEqual(still_open["formula"], "CountIf([As of Date] < Now())")
        self.assertIn("By default, include only orders", result["aiContext"])
        self.assertEqual(result["verifiedQueries"][0]["question"], "Who are the top customers?")
        blob = json.dumps(result)
        self.assertNotIn("CLIENT_" + "SECRET", blob)
        self.assertNotIn("client_" + "secret", blob)
        self.assertNotRegex(blob, r"sigmacomputing\.com")

    def test_no_display_cased_physical_names(self):
        result = convert(REPRO, facts_as_metrics=True)
        for el in elements(result["spec"]):
            for col in el["columns"]:
                match = re.fullmatch(r"\[([^\]/]+)/([^\]]+)\]", col["formula"])
                if match:
                    self.assertNotIn(" ", match.group(2), col["formula"])

    def test_open_invoice_matches_the_certified_metrics(self):
        path = ZYWAVE / "work" / "zip_fixed" / "SV_OPEN_INVOICE_SNAPSHOT.sql"
        if not path.exists():
            self.skipTest("Zywave DDL is not on this machine")
        catalog = json.loads((ZYWAVE / "work" / "catalog_types.json").read_text())
        skip = json.loads((ZYWAVE / "skip_facts" / "SV_OPEN_INVOICE_SNAPSHOT.json").read_text())
        result = convert(path.read_text(), catalog=catalog, skip_facts=skip, facts_as_metrics=True)
        self.assertTrue(result["ok"], [f for f in result["findings"] if f["level"] == "error"])
        el = elements(result["spec"])[0]
        formulas = {m["name"]: m["formula"] for m in el["metrics"]}
        self.assertEqual(
            formulas["Total Balance"],
            "Sum(If([As of Date] = Max([As of Date]), [Invoice Balance]))",
        )
        self.assertEqual(
            formulas["Invoice Count"],
            "Count(If([As of Date] = Max([As of Date]), [Invoice ID]))",
        )
        self.assertEqual(
            formulas["Avg Days Outstanding"],
            "Avg(If([As of Date] = Max([As of Date]), [Days Outstanding]))",
        )
        balance = next(c for c in el["columns"] if c["name"] == "Invoice Balance")
        self.assertEqual(balance["formula"], "[VW_OIA_DATA/INVOICE_BALANCE]")
        self.assertEqual(balance["format"]["formatString"], "$,.2f")
        self.assertNotIn("ACCOUNT_ARR", {m["id"] for m in el["metrics"]})

    def test_every_zywave_view_resolves(self):
        folder = ZYWAVE / "work" / "zip_fixed"
        if not folder.exists():
            self.skipTest("Zywave DDL is not on this machine")
        catalog = json.loads((ZYWAVE / "work" / "catalog_types.json").read_text())
        for path in sorted(folder.glob("*.sql")):
            result = convert(path.read_text(), catalog=catalog, facts_as_metrics=False)
            errors = [f for f in result["findings"] if f["level"] == "error"]
            self.assertEqual(errors, [], path.name)
            spec_elements = elements(result["spec"])
            all_ids = {c["id"] for el in spec_elements for c in el["columns"]}
            index = _catalog_index(catalog)
            for el in spec_elements:
                names = {c["name"] for c in el["columns"]}
                known = _columns_for(el["source"]["path"], index)
                for col in el["columns"]:
                    match = re.fullmatch(r"\[([^\]/]+)/([^\]]+)\]", col["formula"])
                    if not match:
                        continue
                    hit = known.get(_norm(match.group(2)))
                    if hit:
                        self.assertEqual(match.group(2), hit[0], f"{path.name} {col['formula']}")
                for metric in el.get("metrics") or []:
                    for ref in re.findall(r"\[([^\]/]+)\]", metric["formula"]):
                        self.assertIn(ref, names, f"{path.name} {metric['name']} -> {ref}")
                for rel in el.get("relationships") or []:
                    ids = {c["id"] for c in el["columns"]}
                    for key in rel["keys"]:
                        self.assertIn(key["sourceColumnId"], ids)
                        self.assertIn(key["targetColumnId"], all_ids, f"{path.name} {rel['name']}")

    def test_null_tests_and_date_diff(self):
        sql = """
        create or replace semantic view DEMO.ANALYTICS.SV_SPAN
            tables (T as DEMO.ANALYTICS.T)
            dimensions (
                T.NAME as NAME,
                T.START_DATE as START_DATE,
                T.END_DATE as END_DATE
            )
            metrics (
                T.MISSING as COUNT(DISTINCT CASE WHEN T.NAME IS NULL THEN T.NAME END),
                T.SPAN as AVG(DATEDIFF('DAY', T.START_DATE, T.END_DATE))
            )
            facts (
                T.HAS_NAME as T.NAME IS NOT NULL
            );
        """
        result = convert(sql, facts_as_metrics=False)
        self.assertTrue(result["ok"], result["findings"])
        el = elements(result["spec"])[0]
        formulas = {m["name"]: m["formula"] for m in el["metrics"]}
        columns = {c["name"]: c["formula"] for c in el["columns"]}
        self.assertEqual(formulas["Missing"], "CountDistinct(If(IsNull([Name]), [Name], null))")
        self.assertEqual(formulas["Span"], 'Avg(DateDiff("day", [Start Date], [End Date]))')
        self.assertEqual(columns["Has Name"], "IsNotNull([Name])")

    def test_spaced_comment_using_pin_and_undeclared_key(self):
        sql = """
        CREATE OR REPLACE SEMANTIC VIEW ANALYTICS.MODELS.SALES_VIEW
          TABLES (
            lineitem AS SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.LINEITEM,
            orders AS SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.ORDERS PRIMARY KEY (o_orderkey)
          )
          RELATIONSHIPS (
            lines_to_orders AS lineitem (l_orderkey) REFERENCES orders (o_orderkey)
          )
          FACTS (
            PRIVATE lineitem.net_price AS l_extendedprice * (1 - l_discount)
              COMMENT = 'Discounted line price'
          )
          METRICS (
            lineitem.total_revenue AS SUM(net_price)
              COMMENT = 'Revenue net of discount',
            lineitem.revenue_by_order using (lines_to_orders) as SUM(net_price)
          );
        """
        result = convert(sql, facts_as_metrics=False)
        self.assertTrue(result["ok"], result["findings"])
        els = {el["id"]: el for el in elements(result["spec"])}
        lineitem = els["lineitem"]
        net = next(c for c in lineitem["columns"] if c["name"] == "Net Price")
        self.assertEqual(net["formula"], "[L Extendedprice] * (1 - [L Dis Count])")
        self.assertEqual(net["description"].split("\n")[0], "Discounted line price")
        key = next(c for c in lineitem["columns"] if c["formula"] == "[LINEITEM/L_ORDERKEY]")
        rel = lineitem["relationships"][0]
        self.assertEqual(rel["keys"][0]["sourceColumnId"], key["id"])
        orders_key = next(c for c in els["orders"]["columns"] if c["formula"] == "[ORDERS/O_ORDERKEY]")
        self.assertEqual(rel["keys"][0]["targetColumnId"], orders_key["id"])
        self.assertEqual(len([c for c in els["orders"]["columns"] if "O_ORDERKEY" in c["formula"]]), 1)
        codes = [f["code"] for f in result["findings"]]
        self.assertIn("USING", codes)
        revenue = next(m for m in lineitem["metrics"] if m["name"] == "Revenue by Order")
        self.assertEqual(revenue["formula"], "Sum([Net Price])")

    def test_unqualified_table_asks_for_database_and_schema(self):
        sql = """
        create or replace semantic view DEMO.SV
            tables (ORDERS as ORDERS)
            dimensions (ORDERS.ID as ID);
        """
        missing = convert(sql)
        self.assertIsNone(missing["spec"])
        self.assertIn("database", missing["needs"])
        self.assertIn("schema", missing["needs"])
        filled = convert(sql, database="DEMO", schema="ANALYTICS")
        self.assertEqual(elements(filled["spec"])[0]["source"]["path"], ["DEMO", "ANALYTICS", "ORDERS"])

    def test_package_has_no_credentials(self):
        banned = ("CLIENT_" + "SECRET", "client_" + "secret", "SIGMA_" + "CLIENT", "api_" + "key")
        for path in ROOT.rglob("*"):
            if path.suffix not in {".py", ".html", ".css", ".js", ".md", ".sql", ".json"}:
                continue
            if "__pycache__" in path.parts:
                continue
            # The data model manager page is the original UI, including its Sigma connect form.
            if path.name == "index.html":
                continue
            text = path.read_text(errors="ignore")
            for word in banned:
                self.assertNotIn(word, text, f"{path} contains {word}")


if __name__ == "__main__":
    unittest.main()
