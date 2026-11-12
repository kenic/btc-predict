# Phase 8pre — English news collection, independent of predictions

## Scope and existing environment

Inspected `kenic/btc-predict` main at `661a068`. Existing Python/Flask
prediction, evaluation, market collectors and `candles.db` archive remain
unchanged. The only existing-file edits are this README link and ignore rules.
The repository documents Linux systemd under `/opt/btc-predict`, using `.venv`.
The supplied production address is `ubuntu@nepp:/opt/btc-predict`. After the user
installed the SSH public key, read-only inspection confirmed Python 3.12.3,
`btc-web.service` and `btc-candles.service` running as `ubuntu:ubuntu`, and active
Phase 5b/6b/6d/7, Claude observation and candle timers. Production main is
`d5dca83`, 13 commits ahead of GitHub main, including prospective Phase 7 selector
work. Those commits were fetched without modifying the server and are retained
in a separate compatibility-validation checkout. Existing untracked production
plots and candle WAL/SHM files are left alone. No applicable AGENTS.md was found.
With the user's explicit approval, a separate `/opt/btc-news` deployment and
dedicated `btc-news.service`/`btc-news.timer` were installed on 2026-11-11 JST.
The original `/opt/btc-predict` checkout, databases and services remain untouched;
no existing service restart, migration or prediction call was performed.

New code needs only Python **3.9+ standard library** on Linux/macOS. In particular
it does not import Flask, pandas, OpenAI, TypeSafe, experiment runners or `.env`.
Existing application dependencies and model configuration are not modified.

## Quick start

From the repository directory, using a local copy of the example:

```sh
cp news.config.example.json news.config.json
python3 -m news.cli --db news.db collect --config news.config.json
python3 -m news.cli --db news.db health --format text
python3 -m news.cli --db news.db health
python3 -m news.cli --db news.db snapshot --as-of '2026-11-11T17:10:00+09:00'
python3 -m news.cli --db news.db snapshot --as-of 1794384600 --keep-duplicates
python3 -m unittest tests_news.test_news -q
```

`collect` runs once. The timer supplies recurrence. Read commands never fetch,
create missing stores or initialize/migrate a database. A requested time before
initial collection returns no articles; it cannot reconstruct unobserved history.
The sample date is illustrative: use a time after your actual first ingestion.
CLI refuses future `as-of` times. Timestamps are UTC Unix seconds (publication
precision seconds, local ingestion precision fractional seconds). Naive/invalid
publisher dates are stored as unknown and excluded from the default news window.

Exit codes: 0 = completed, 1 = failed/deferred feed or rejected entries (health:
enabled source stale/failed/deferred/rejected), 2 = configuration/storage/lock
error. A disabled source is explicitly reported as `blocked`, without a request.
An empty feed is a successful response with `entries=0`, visible in health JSON;
it does not prove there was no news. Logs are structured JSON on stderr/stdout
and contain source ids, run ids, statuses and counts, never exception messages,
response text, request URLs or headers. Detailed safe HTTP/error categories are
in SQLite/health. The default government feeds require no credentials.

## Source permissions reviewed 2026-11-11

| Feed | Default | What is retained | Basis / remaining question |
|---|---|---|---|
| CoinDesk RSS | **Blocked** | Nothing fetched | [Official RSS announcement](https://www.coindesk.com/coindesk-news/2021/09/17/coindesk-rss) exists, but [Terms of Use](https://www.coindesk.com/terms), effective 2026-10-30, require express written permission for automated access and restrict reuse. Whether RSS grants an exception for automated research archiving/AI input is unclear. |
| FRB all press releases | Enabled | RSS bytes, entry XML, title/summary/URL/dates | [Board disclaimer](https://www.federalreserve.gov/disclaimer.htm): Board information public domain unless indicated otherwise; retain attribution, exclude third-party assets. [RSS directory](https://www.federalreserve.gov/feeds/feeds.htm). |
| BLS CPI, Employment Situation, JOLTS | Enabled | Same RSS-only scope | [BLS copyright statement](https://www.bls.gov/opub/copyright-information.htm): publications public domain except previously copyrighted photographs/illustrations; attribution requested. [RSS directory](https://www.bls.gov/feed/). |

**No linked article page is fetched, and no separate article body is stored.**
The raw response necessarily includes whatever the licensed RSS publisher embeds;
only title/description/summary becomes normalized model-input text. Government
feeds can contain embedded content or links; no media/link targets are downloaded.
Source attribution is stored with each version and exported. These checks do not
grant rights to third-party copyrighted material, logos, downstream republication
or a later change of terms. Review new feed notices before enabling additional
sources. This implementation is private research storage, not a public RSS proxy.

For CoinDesk, obtain written permission covering automated retrieval, raw archival
RSS (including embedded content), summaries and intended third-party model use.
Then record its actual basis/reference in the local config, set `enabled=true`,
`policy.mode="rss"` and `model_input` according to that permission. Merely changing
the flags does not establish permission. No CoinDesk HTTP request was made in
the live validation. API keys/credentials must not appear in configuration URLs,
policy text or logs. Authenticated CryptoPanic/GDELT adapters are not implemented;
future adapters must load credentials from the environment, redact errors and
retain only data allowed by their contracts. Existing secret files stay untouched.

## Storage, identity and updates

`news.db` is a new, dedicated SQLite database with application id `NEWS`, schema
version 1, WAL, foreign keys and a five-second busy timeout. Paths named `btc.db`,
`market.db` and `candles.db`, as well as existing foreign database schemas, are
refused before changing journal/schema settings. There is no migration of existing
experiment databases. Keep the DB/WAL/SHM and collector lock beside each other;
take consistent backups using SQLite's backup API, not a live DB file copy alone.

- `feeds`: immutable first configured time for each feed id.
- `runs`: start/completion/availability times, a snapshot of complete source
  metadata/policy, interval, status, HTTP validators, raw response bytes/SHA-256,
  entries/rejections/duplicate/revision counts. Failed response bodies are omitted.
- `attempts`: every HTTP attempt, safe failure category, HTTP status, persistent
  Retry-After deadline. No request headers or authentication data are stored.
- `articles` / `aliases`: per-feed identity using GUID and canonical URL aliases;
  first/last actually observed times. GUID changes and moved links can remain one
  article when either alias matches. Conflicting aliases are rejected, not merged.
- `versions`: append-only raw-entry and normalized content hashes, metadata,
  source run, local availability time. Raw entry XML is reserialized; exact original
  bytes and namespace formatting remain available in `runs.raw_body`.
- `observations`: article/version presence in each successful response, including
  conditional 304 confirmations. Absence from a later feed is not a deletion.

Only tracking parameters (`utm_*`, `fbclid`, `gclid`) and fragments are removed
from canonical URLs; semantic query parameters remain. Arbitrary secrets in URL
credentials or common credential query keys are rejected. Exact canonical URLs
identify cross-feed duplicates at **query** time while preserving every source's
observations. Same headlines or similar topics are never automatically merged.
Conflicting same-response URL/GUID versions are rejected together. Source metadata
or policy changes create a new provenance version even with identical entry XML.
A->B->A produces three transitions; it never reuses A's old availability time.
Repeated unchanged replies update last-seen observations without new versions.

## Point-in-time contract and limits

For cutoff `T`, choose each article's latest locally ingested version with
`available_at <= T`, ordered by `(available_at, version_id)`. Only then filter its
publication time to **`T-24h < published_at <= T`** and its recorded model-input
permission. A future-dated/invalid revised publication time cannot cause fallback
to an older otherwise eligible version. Publisher `updated` and HTTP Last-Modified
are metadata, never availability clocks. `first_fetched_at`/`last_fetched_at` in
the output are also bounded by T; mutable current `articles.last_seen_at` is never
used for historical reads. A late article can enter a later snapshot but can never
be injected into earlier snapshots. Publication correction is versioned too.

Network and parsing finish before availability is stamped in the short SQLite
ingestion transaction. This is the local observation time, not the publisher's
claimed time and not the HTTP request start. Wall-clock regressions fail the run
instead of backdating data. Maintain UTC clock synchronization on the collector.
SQLite commit visibility can lag that timestamp by the duration of the short
write transaction; for prediction experiments, freeze/export the actual snapshot
before invoking engines and retain that exact JSON/hash as the definitive input.
No historical query can prove sub-transaction database visibility from timestamps
alone. This collector provides observation-based replay, not an internet-wide
publication archive or a guarantee of every intermediate publisher revision.

`snapshot` returns `phase8pre.news.v1` with cutoff/window bounds, selection policy,
all eligible news and per-version provenance, publication-to-acquisition lag,
exclusion counts, and historical polling coverage. It applies no BTC keywords,
ranking, scoring, top-N limit, sentiment model or model-specific prompt. A future
selection module must consume this frozen set and record its rule/version.
`--keep-duplicates` exposes all source copies with `duplicate_of` annotations;
default exact-URL deduplication is deterministic within the eligible set.

## Retry, outages and visibility

HTTPS requests use public endpoints, 15-second socket timeouts, a 4 MiB response
limit, at most three attempts with exponential backoff/jitter, and same-host HTTPS
redirects only. No TLS bypass, proxy workaround, article scraping or 403 bypass.
XML DTD/entities and UTF-16/32 are refused. RSS 2, RSS/RDF and Atom entry fields
are supported; timezone ambiguity remains unknown rather than guessed.

Network errors and HTTP 408/425/429/500/502/503/504 retry. Other 4xx and invalid XML
fail once. Retry-After seconds/HTTP dates are respected; delays over ten seconds
defer to a later timer cycle, persisting the deadline across process restarts.
ETag/Last-Modified validators are reused only for an unchanged source config and
the last successfully parsed 200 body. A 304 without one is a failure. A failing
feed does not prevent other feeds from collecting. Storage/lock failures end the
cycle. Nonblocking advisory locking prevents concurrent collectors using the same
DB path. A killed process leaves a visible unfinished `running` record; restarting
does not rewrite it as successful. The timer retries at its next ordinary firing.

`health --format text` provides a terminal table; JSON adds raw counts, last safe
error/HTTP code, Retry-After deadline, unfinished runs and gap ranges. A gap is an
interval between successful observations exceeding 1.5 configured polling periods;
the first-configured-to-first-success and last-success-to-now intervals count too.
Staleness uses the same threshold. Duplicate rate includes unchanged repeated
observations (normal for sparse government releases), not just duplicate articles.
These are **polling coverage gaps**, not an asserted count of missing stories.
Feeds have finite retention: articles or revisions that appeared and disappeared
between successful polls may be permanently lost. No invented backfill timestamps,
interpolation or hidden automatic history import is performed. News present in
the first response may be archived with old publication dates and today's actual
first acquisition time. Sparse monthly feeds need not publish every 24 hours.

## Linux deployment and rollback

Production collection is now enabled in the independent directory described below.
The following alternative recipe shows installation beside a reviewed repository
checkout. Do not install a second copy over the active deployment. The template
uses the observed `ubuntu:ubuntu` account. Confirm branch, account,
paths and local changes before installing. Leave all existing timers alone.

```sh
cd /opt/btc-predict
git status --short --branch
systemctl show btc-web.service -p WorkingDirectory -p ExecStart -p User -p Group
# Bring the reviewed Phase 8pre code into this checkout using your normal process.
.venv/bin/python -m unittest discover -q
cp news.config.example.json news.config.json
chmod 600 news.config.json
# Review feed permissions/config first; CoinDesk remains blocked.
.venv/bin/python -m news.cli --db news.db collect --config news.config.json
.venv/bin/python -m news.cli --db news.db health --format text
sudo install -m 0644 deploy/systemd/btc-news.service /etc/systemd/system/btc-news.service
sudo install -m 0644 deploy/systemd/btc-news.timer /etc/systemd/system/btc-news.timer
sudoedit /etc/systemd/system/btc-news.service
# Set User/Group to the verified unprivileged account and adjust paths as needed.
sudo systemd-analyze verify /etc/systemd/system/btc-news.service /etc/systemd/system/btc-news.timer
systemd-analyze calendar '*-*-* *:00/15:00 UTC'
sudo systemctl daemon-reload
sudo systemctl enable --now btc-news.timer
systemctl list-timers btc-news.timer
systemctl show btc-news.service -p Result -p ExecMainStatus
journalctl -u btc-news.service -n 50 --no-pager
```

Timer is every 15 minutes UTC, `Persistent=false`, service timeout 360 seconds,
private file permissions, no dependencies on experiment locks/runners/services.
For ten-minute polling change both config interval to 600 and the timer calendar
to `*:00/10:00 UTC`, then reload/restart only the news timer. No web restart is
needed. Unprivileged owner must have write access to the DB directory; restricting
that account to a separate data directory is supported with `--db` and matching
`ReadWritePaths`. Do not point the collector at existing experiment DBs.

Rollback: disable/stop only `btc-news.timer`, allow any news oneshot to finish,
and keep `news.db` for audit/replay. Existing prediction/web/market services need
no restart or data restoration. No destructive retention policy is installed;
raw responses grow with poll frequency. Measure disk usage and back up regularly.

## Phase 8a contract proposal (design only)

`news/phase8a-output.schema.json` is a JSON Schema 2020-12 contract shared by
logical engine labels `gpt6_astra`, `claude_fable`, `jev`, `kairos`. These are
experiment labels from the requested design, **not verified provider API model
identifiers**. `news/phase8a-output.example.json` is an abstention example; no
prediction is generated. Four-hour horizon is fixed at 14400 seconds; target is
BTC-USD. One probability `probability_up` avoids inconsistent probability sums;
`probability_down = 1 - probability_up`. UP means p_up >= .5, DOWN means < .5.
Future runtime must validate direction consistency, evidence/version membership,
identical cutoff/input hash, and prediction deadline. Schema validates shape and
ranges; cross-field/set membership rules belong in that future runtime.

Freeze one common input containing the selected news snapshot and the same market
snapshot for all engines. Hash the entire frozen input as UTF-8 JSON using sorted
keys, `ensure_ascii=False`, `separators=(',', ':')`, `allow_nan=False`, SHA-256.
Retain actual bytes, selection-rule version, prompt version, actual provider/model
identifier, configuration, request/response times, validated result and failures
in Phase 8a's run metadata. `news_versions` lists exact archive version ids;
evidence references must be a subset. `ok` requires a prediction; `abstain` and
`error` require null. Keep news as untrusted data, never prompt instructions.
No engine SDK, API request, forecast scheduling or existing experiment mutation
is introduced here.

## Verified standalone production deployment

On 2026-11-11 JST, the user explicitly authorized live collection. Installed only
new files under `/opt/btc-news`, owned by `ubuntu`, with config mode 0600 and
directory mode 0750. The deployed service renders the repository template with:

```ini
WorkingDirectory=/opt/btc-news
ExecStart=/usr/bin/python3 -m news.cli --db /opt/btc-news/news.db collect --config /opt/btc-news/news.config.json
ReadWritePaths=/opt/btc-news
```

It uses system Python 3.12.3 and standard library only. No existing environment
was changed. The code source commit is
`d229f5e9557c43efd40558f72b32cbbb2769ed32`; `SOURCE_COMMIT` and
`source-manifest.json` retain its per-file hashes. Later documentation commits do
not change that deployed code. Active units are `/etc/systemd/system/btc-news.*`.
The fifteen-minute UTC timer is enabled, and initial service Result is success,
ExecMainStatus 0. CoinDesk remains blocked. The initial FRB/BLS run stored 56
articles; repeated collection returned 304 for all four feeds without adding
versions. Past-cutoff snapshot equality is checked across the repeated collection.
The original production checkout remains at `d5dca83` with its preexisting
untracked plots/WAL files and no tracked edits. Existing web/candle/Phase 7
services were rechecked as active.

Inspect the active deployment directly:

```sh
cd /opt/btc-news
/usr/bin/python3 -m news.cli --db news.db health --format text
/usr/bin/python3 -m news.cli --db news.db snapshot --as-of '2026-11-11T18:00:00+09:00'
systemctl list-timers btc-news.timer
journalctl -u btc-news.service -n 50 --no-pager
```

Choose a cutoff that has already occurred. No web route was added; CLI JSON and
the terminal health table provide collection/query visibility independently.
Rollback remains `sudo systemctl disable --now btc-news.timer`; retain the DB.
Do not modify the archived version timestamps or delete the collection history.

## Validation / completion checklist

- Implemented: isolated collection/storage, 15-minute timer (10-minute option),
  metadata/raw RSS, first/last acquisition, version history including reversion,
  same-/cross-source duplicate handling, point-in-time 24h queries, independent
  selection boundary, retry/log/coverage reports, config/docs and output schema.
- Automated coverage includes delayed responses, late arrivals, revisions,
  future publication correction, date/timezone/window boundaries, A->B->A,
  historical last-seen/source metadata/health, unchanged/304 replies, changed
  GUID/URL, ambiguous duplicates, Retry-After restart, feed failure isolation,
  CoinDesk policy blocking, malformed XML, lock/interruption, clock regression,
  read-only queries and preservation of foreign/experiment DB bytes.
- Results: 31 new standard-library tests pass; GitHub main plus this change passes
  112 tests locally on Python 3.9.6. The actual production `d5dca83` code plus this
  change passes all 209 tests on the server's Python 3.12.3 in an isolated Git
  clone. JSON Schema/four engine examples and Linux systemd units/calendar pass.
  An initial archive-only test copy lacked Git metadata and caused 44 existing
  registration tests to fail; rerunning in a proper isolated clone resolved all
  failures. Existing static-file ResourceWarnings remain non-failing. The pinned
  production dependency set was used for the Linux run; local tests used available
  compatible dependencies because not all pinned packages exist on the local index.
- Live FRB/BLS collection is explicitly permitted by configured policy; CoinDesk
  and actual prediction calls are excluded. See the accompanying verification
  report for actual test counts, live responses and environment limitations.
- Pending external condition: CoinDesk written permission. Future CryptoPanic/GDELT adapters and
  Phase 8a model calls are intentionally outside this implementation.
