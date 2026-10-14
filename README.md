# India Open Budget Portal

The frontend is served by a local Python application. It uses Python's standard
library for the HTTP/JSON API, CSV import, and data processing, and SQLite for
the local SQL database. No third-party Python packages are required.

## Interface languages

The interface language selector is available on the homepage and dashboard.
English, Hindi, Marathi, Bengali, Tamil, Kannada, and Telugu are supported using
the local `translations.js` dictionaries; no external translation service is
used. The selected language is saved in the browser. Text without a translation
remains in English.

## Run locally

From this folder, run:

```powershell
py -3 app.py
```

If the Python launcher is unavailable, run `python app.py` with Python 3.10 or
newer, or double-click `run.bat` on Windows. Open <http://127.0.0.1:8000> in a
browser. Stop the server with `Ctrl+C`.

The app creates `data/budget.db` on first run and idempotently imports the
bundled public starter dataset in `data/budget_seed.csv`. Keep this CSV in the
project's `data` folder when downloading or copying the project; without it, a
fresh database cannot be populated and the budget tables will be empty. Set
`PORT` to change the port, or `BUDGET_DB_PATH` to store the SQLite database
elsewhere.
Backups are written to `data/backups` by default; set `BUDGET_BACKUP_DIR` to
change that location. A validated backup is created when the server starts and
once every 24 hours while it remains running. The automatic rotation retains
the latest 30 backups.

## Web sign-in (local sample)

Citizen browsing is public and does not require an account. The login panel
also supports optional citizen account creation using an email address and
user-chosen password (8-128 characters). Login and registration require a
server-issued, expiring, single-use CAPTCHA; credentials and CAPTCHA answers
are validated by the backend.

On loopback-only sample runs, the app seeds these demo credentials at startup:

- Central Admin: `centraladminUN` / `india@`
- State Auditor: `stadminmh` / `india@` for Maharashtra. Replace `mh` with the
  state's two-letter catalogue code in lowercase for other states (for example,
  `stadminbr` for Bihar).

Central Admin has all-state scope. Each State Auditor is scoped to their state
and districts in the dashboard and administrative CSV staging/correction
commands. Demo accounts are deliberately sample-only: the shared `india@`
password is weak and must not be reused on a public or hosted deployment. Demo
accounts are not seeded if the app binds to a non-loopback `HOST`. Browser
sessions use an HTTP-only, SameSite cookie held in server memory, so signing
out or restarting the local server ends the session.

The local demo sign-in does not provide identity verification, email
verification, password reset, production rate limiting, or a hosted identity
provider. Do not expose this sample server to the internet.

## API

- `GET /api/health` reports server and database readiness.
- `GET /api/coverage` returns the Union/state/UT/district hierarchy,
  source-backed, user-provided, and illustrative figure counts, available
  fiscal years, and budget stages.
- `GET /api/budget?jurisdiction_code=IN-MH&fiscal_year=2026-27` returns
  figures and stage-separated totals. Other-state/UT synthetic rows and their
  totals are explicitly identified as illustrative and not official.
- Optional filters: `budget_stage=budget_estimate|revised_estimate|actual|funds_received`,
  `district=Pune`, `sector=education|agriculture|roadways_transport|healthcare|unclassified`,
  `department=education`, and `q=education`.
- `GET /api/compare?jurisdiction_codes=IN-MH,IN-BR&fiscal_years=2025-26,2026-27`
  compares up to 12 states/UTs across selected years by sector, stage, amount unit, and provenance.
  Optional filters: `budget_stage`, `sectors` (comma-separated sector slugs), and `department`.
  The interface provides dropdowns with checkboxes for states/UTs, fiscal years,
  and sectors. Select Education, Transport, Agriculture, and Healthcare
  independently; any signed-in role can compare public state/UT budget data
  across jurisdictions. Sector totals stay separate. It does not merge unlike
  units or provenance groups.

The dashboard begins on a searchable India state/UT explorer; selecting a state
opens its district list and map. “Compare Budgets” in the sidebar opens a
separate comparison page. Fiscal year, budget stage, sector, and original
department filters apply to the ledger. “Download filtered CSV” exports the
currently filtered ledger rows. The ledger shows `actual`-stage amounts in a
**Amount** column like every other stage; **Spent** shows the separately
imported spent amount when one is provided. The comparison page supports
multi-state, multi-year selection with its own CSV download and pie charts showing each
selected year's Amount shares by state/UT, plus bars for reported Spent
amounts. Activity charts also show Amount and Spent as separate bars.
Separate sector selections are available for Education, Transport, Agriculture,
and Healthcare. Chart panels keep sector,
budget stage, currency,
unit, and data provenance separate; pies include only selected jurisdictions
with reported positive amounts for that year. State comparison totals include
available district figures; overlapping state/district aggregates may not be
additive. Synthetic and user-provided data stay labelled and separated.

## Importing user-provided data

The portal does not download, discover, or certify government budget datasets.
The existing Maharashtra source rows remain as they are; the other-state
synthetic rows remain visibly labelled as illustrative. To add figures, use a
dataset you provide. A recorded internal review is not proof that the source
is official or authentic.

The supported import is UTF-8 CSV. Start from
[`data/budget_import_template.csv`](./data/budget_import_template.csv). Required
columns are:

Required columns are `jurisdiction_code`, `jurisdiction_name`, `fiscal_year`, `budget_stage`,
`department_name_original`, `sector_name_original`, `sector_category_slug`,
`amount`, `currency_code`, `amount_unit`, `source_page_or_table`,
`source_locator`, and `correction_notes`. `spent_amount` is an optional column.

`spent_amount` may be omitted or left blank when the source does not report a
spent value. When supplied, it appears in the
budget table's **Spent** column; **Amount** always remains the imported `amount`.
Use the exact jurisdiction code and name from the catalogue. Fiscal year,
stage, sector category, state/district code/name consistency, finite
non-negative amounts, currency, and duplicate rows are validated before a
batch is staged. Leave both sector fields blank when the source does not
provide a sector breakdown. Every row needs a source page/table and locator.
The batch records the source document, optional URL, file SHA-256, import time,
affected jurisdictions/years, authenticated importer, and dataset-level
corrections. Staged records can be corrected field-by-field before review; each
change requires a reason and is captured in the audit log.

### Administrative access

Central Admin and State Auditor accounts can use the dashboard's **Import
Budgets** page to upload a UTF-8 CSV (up to 5 MB), inspect staged batches, and
correct staged amounts with an audit note. The page is hidden for citizens and
other roles; the import API independently checks authorization on every
request. Central Admin can import across jurisdictions. A State Auditor can
stage or correct only data for their assigned state and its districts; another
state's auditor cannot view or change that batch. The
[`sample-budget-import.csv`](./sample-budget-import.csv) download contains one
fictional example row to demonstrate the required fields. Replace all sample
values with verified source data and do not publish the example.
District examples for [`Ajmer`](./sample-rajasthan-ajmer-budget-import.csv)
(`IN-RJ-D-AJMER`) and
[`Jodhpur`](./sample-rajasthan-jodhpur-budget-import.csv)
(`IN-RJ-D-JODHPUR`) use Rajasthan's district catalogue codes. Both are entirely
fictional; replace all sample values with verified source data before importing.
Illustrative district rows across states include fictional demo Spent values
(60% of the illustrative amount) so the separate column can be demonstrated;
these are visibly marked “demo” and are not official expenditure figures.

Only Central Admin can publish staged batches. A different administrator must
review the batch than the person who staged it. Reviewers can review but cannot
publish; after review, Central Admin performs the final publication step.
Until publication, staged rows are not included in budget queries. Each row
correction requires a reason and is
recorded in the audit log. For comprehensive field-by-field corrections, the
command-line `correct` workflow below remains available. The first CLI-only
admin account, if needed, is bootstrapped on the trusted local machine.
Passwords for these accounts are entered interactively (not as command
arguments), salted, and stored as PBKDF2-HMAC-SHA256 hashes. CLI accounts have
roles:

- `data_entry`: stage imports, correct staged rows, and inspect batches.
- `reviewer`: independently review and inspect batches. Only Central Admin can
  publish reviewed batches.
- `admin`: stage, correct, review, and inspect batches, plus account management,
  audit inspection, and backups. This CLI role cannot publish.

The stager cannot review their own batch. A Central Admin publishes a reviewed
batch, and a reviewed batch cannot be edited; corrections after review require
a new import. The last active admin cannot be disabled or demoted.
These local controls are a prototype safeguard, not a substitute for an
identity provider or hardened production deployment.

Create the first admin, then separate data-entry, reviewer, and Central Admin
accounts:

```powershell
py -3 import_budget.py create-user admin --role admin
py -3 import_budget.py create-user data-entry --role data_entry --admin-user admin
py -3 import_budget.py create-user reviewer --role reviewer --admin-user admin
py -3 import_budget.py create-user central-admin --role central_admin --admin-user admin

py -3 import_budget.py stage .\my-budget.csv `
  --source-document "Provided budget workbook.xlsx" `
  --source-url "https://example.invalid/source.xlsx" `
  --username data-entry `
  --corrections "None declared."
py -3 import_budget.py list --username reviewer
$batchId = "paste-the-batch-id-printed-by-stage"
py -3 import_budget.py show $batchId --username reviewer
py -3 import_budget.py correct $batchId 2 `
  --username data-entry `
  --amount 123.46 `
  --correction-note "Corrected against the cited source table."
py -3 import_budget.py review $batchId `
  --username reviewer `
  --notes "Compared sample rows and totals against the cited source tables."
py -3 import_budget.py publish $batchId `
  --username central-admin `
  --confirm-batch $batchId
```

Staged rows do not appear in budget queries. Only a reviewed batch can be
published; failed validation, reviewer mismatch, duplicate published figures,
or an incorrect confirmation ID stops publication. Publishing is
transactional. Published table rows carry the dataset name/URL, page/table,
locator, import date, corrections, reviewer, and batch ID. An imported dataset
with data for a district/year/stage replaces the synthetic demo rows for that
same district/year/stage in queries.

Administrative account management commands include `users --username admin`,
`set-role USER --role ROLE --username admin`, and
`disable-user USER --username admin`. Do not share admin credentials. The
reviewer should inspect the complete source, reconcile totals, names, units,
fiscal year and stages. The portal records the review trail but does not
independently authenticate the data or source provider. Excel files must
currently be exported to UTF-8 CSV. PDF extraction is not implemented:
transcribe to CSV and review it manually; no PDF content is published
automatically.

### Audit and recovery

Batch staging, corrections, review, publishing, account/role changes,
authentication failures, and backups are recorded in an append-only SQLite
audit table. Entries are SHA-256 hash-chained; SQLite triggers prevent normal
updates/deletes, and admins can inspect or verify the chain:

```powershell
py -3 import_budget.py audit --username admin --limit 100
py -3 import_budget.py verify-audit --username admin
py -3 import_budget.py backup --username admin
```

Backups use SQLite's online backup API and pass an integrity check before
being retained. Keep copies on storage separate from the database machine for
disaster recovery; the local hash chain and same-machine backups are not
tamper-proof against someone with filesystem/database-owner access. Stop the
app before restoring a verified backup. For the local prototype, SQLite is
appropriate; consider PostgreSQL when concurrent users, larger datasets, or a
hosted production deployment require it. Before network/hosted use, replace
the local CLI bootstrap model with a production identity provider, enforce
server-side role permissions on every administrative endpoint, and deploy
behind HTTPS with secure session, secret-management, monitoring, and backup
policies.

## Data model and coverage

SQLite stores a parent-child jurisdiction hierarchy (`union`, `state`,
`union_territory`, `district`), standardized sector categories, and one row per
budget figure in `budget_figures`. Each figure has a fiscal year, budget stage,
original department and sector labels, standardized sector slug, numeric
amount, currency code, amount unit, source and source locator. The source labels
are retained; missing source department/sector classifications are not
invented. `funds_received` is available as a separate source-reported stage and
is not treated as a Budget Estimate or Revised Estimate.

The catalogue contains the Union Government, all 28 states, all 8 Union
Territories, and 735 districts for all states and UTs. The official [Maharashtra BEAMS
District-wise Expenditure report](https://beams.mahakosh.gov.in/Beams5/BudgetMVC/MISRPT/DistrictWiseExpenditure.jsp)
provides 2026–27 Budget Received and Expenditure rows for 34 districts (68
figures). Mumbai City and Mumbai Suburban are catalogued, but have no rows in
that report. Non-district treasury rows are excluded.

For the other 699 districts, `data/budget_seed.csv` includes eight generated
FY 2026–27 demonstration rows per district: Budget Received and Expenditure
for each of Education, Agriculture, Healthcare, and Transport. The department
names and near-equal allocation of each previous district/stage demo total
across those four labels are **synthetic examples, not source-reported
departments, budgets, or expenditure**. The split preserves each district's
previous illustrative stage total to the cent. These values exist only to
demonstrate the same district table, filters, and map-click behavior across the
country. They must not be used for financial decisions, cited as government
figures, or combined with sourced Maharashtra totals as if they were real. The
UI labels each row, amount stage, source note, coverage badge, and subtotal as
illustrative. The stable demo values are stored in the CSV; they are not
calculated from real state budgets. The demo Original Sector labels are
Education, Crop Husbandry, Public Health, and Roads and Bridges; their
Standardized Sector categories are respectively `education`, `agriculture`,
`healthcare`, and `roadways_transport`. These sector assignments are also
illustrative, not extracted from a source document.

Interactive district maps are available for all 28 states and 8 Union
Territories. The bundled [SVG map geometry](./india-districts.js) is adapted
from geoBoundaries India ADM2 simplified boundaries (boundary year 2021,
boundary ID `IND-ADM2-76128533`; source: Pathways Data Pvt. Ltd. / Local
Government Directory) and ADM1 boundaries used to associate districts with
their states/UTs (boundary year 2011, boundary ID `IND-ADM1-1811400`; source:
DataMeet India community / Election Commission of India). The ADM2 data is
licensed under [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/); ADM1
data is licensed under
[CC BY 2.5 India](https://creativecommons.org/licenses/by/2.5/in/). The
generated geometry is projected and simplified for browser display, and some
Maharashtra district names are aligned with the budget catalogue (including
Ahilyanagar, Beed, Buldhana, Chhatrapati Sambhajinagar, Dharashiv, Gondia,
Mumbai City, and Raigad). See
[`data/BOUNDARY-DATA-NOTICE.txt`](./data/BOUNDARY-DATA-NOTICE.txt) for source
and attribution details. Clicking a district filters the budget table, but
boundary availability does not imply sourced budget data. Other states/UTs
display the clearly identified illustrative rows described above; missing
years remain explicitly unavailable.

BEAMS does not state the numeric unit or provide department/sector breakdowns.
Its amounts are stored as displayed, with `source_unit_not_stated`; do not
interpret them as rupees, crore, or sector allocations. The report's Expenditure
column is recorded as `actual`, and Budget Received remains `funds_received`.
The other-state/UT rows use the same stage categories for visual consistency,
but their values are synthetic and not actual government spending.

The four 2025–26 presentation examples remain separately available: Maharashtra
Education and Pune Healthcare Budget Estimate/Actual pairs. They are explicitly
examples, not complete budgets. Years other than 2026–27 and stages without
rows remain unavailable, not zero. Internal jurisdiction keys such as `IN-MH`
are application identifiers, not official government codes.

## Tests

Run the backend test suite with:

```powershell
py -3 -m unittest discover -s tests -v
```
