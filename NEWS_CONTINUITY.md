# Shared news continuity update — 2026-11-13 JST

## Finding

At the 2026-11-13 00:00 JST cutoff, BTC had no eligible article from the configured
sources, which caused the entire 20-call 8a cohort to skip and consequently 8b.
CoinDesk was already enabled and operator-approved in 8pre, but missing from the
prediction allowlists. At 00:15, the archive held 19 CoinDesk articles within 24h;
NewsBTC had only one newly arrived article after cutoff. The export and collectors
were functioning; reusing an HTTP 200/304 is not evidence of newly published news.
The previous all-allowed-sources freshness gate also made any one stale feed halt
all forecasts, even when fresh input from another feed existed.

## New prospective condition

All three phases allow CoinDesk, NewsBTC, FRB and the three existing BLS feeds.
Only feeds enabled and not stale in the point-in-time archive coverage can supply
articles. Missing/unknown coverage fails closed. A stale feed is excluded, without
stopping predictions from healthy sources. No article older than 24h, future
publication, late availability or unknown source is admitted. Do not manufacture
news or extend the window to force a call when all sources are empty/unavailable.
Direct-asset priority, deterministic ordering, URL/text deduplication, five-item
cap and 2400-byte envelope remain unchanged. The selection version is now
`asset-direct-common-healthy-sources-v3`.

8a freezes the only authoritative manifest. 8b/8c reuse its exact article and
version IDs, bytes and news_set_id. Model settings, prompt, horizon and technical
conditions are unchanged. All phases move to separate `outputs/news-v2-20261113`
ledgers at a future aligned cutoff. Never delete/reset an existing contract or
reservation. Archive previous config/schedule/source files before updating runtime.
Existing run artifacts are copied byte-for-byte (SQLite databases through consistent
backup) into the new run tree; originals remain untouched. Prior ledger rows, call
counts and budget reservations are carried forward, with lineage metadata instead
of resetting the old contract. Existing standalone additional batches remain listed.
Mature evaluations and full Web history remain available. Aggregates include both source regimes and the Web
explicitly labels the transition; pair comparisons still require matching news IDs.

Collection is every ten minutes at :09/:19/:29/:39/:49/:59 UTC, allowing the final
collection and SQLite reader export to complete before the hourly cutoff. All
first-available timestamps remain actual receipt times. The reader export runs in
ExecStopPost, so committed healthy-feed observations are published even if another
feed makes the collector exit nonzero; feed failures remain visible in health logs. A delayed :59 fetch cannot
be smuggled into that hour; the selection uses the earlier known archive versions.

The Web shows safe feed status/article counts and the current eligible count per
asset, separately from the selected historical prediction. No headlines, raw
responses or credentials are newly exposed. Safe skip categories are retained in
prediction-status.json. No retry of a skipped/reserved historical paid cohort.

## Checks and operation

Run Phase 8a/b/c and Web tests, including stale-feed isolation, missing health,
late arrivals, duplicate handling, shared frozen input, no replay and outcomes.
Validate real archived news with the new config without dispatch, and record
per-asset selection counts/source IDs. Verify the next hourly cohorts separately.
Existing 8c microstructure warm-up still applies; news readiness alone is not
technical readiness. On a fault, stop prediction timers, retain evaluation and all
archives, then restore the previous release/config/schedule and matching Web roots.
Do not erase contracts or bypass the first-observed-time checks.
