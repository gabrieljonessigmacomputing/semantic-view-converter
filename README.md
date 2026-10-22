# Semantic view converter

Public page: https://gabrieljonessigmacomputing.github.io/semantic-view-converter/

The page is the Sigma data model manager. Every converter, the editor, and Save to Sigma work as they do there. The Snowflake Semantic View converter on this copy uses the converter in `converter/`. On the public page that Python runs in the browser, so the first conversion downloads a runtime. Column formulas use the warehouse column name, relationships point at the real key, and a semi-additive metric keeps the latest value in each group.

The Sigma connect form keeps what you type in memory for the session. This repository does not contain an API credential.

## Run locally

```bash
cd /Users/gabrielegjones/.cursor/zywave/semantic-view-converter
python3 server.py
```

Open http://127.0.0.1:8765/ and choose Snowflake Semantic View. Locally, conversion goes through `POST /api/convert`.

## Tests

```bash
python3 -m unittest tests.test_convert -v
```
