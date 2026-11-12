# Phase 8pre dashboard

`/phase8pre/` adds the existing site's shared navigation and styling to a
read-only view of the independently collected news archive. `/phase8pre/data.json`
returns the same bounded, public projection. Configure `BTC_NEWS_DB` if needed;
the default is the deployed `/opt/btc-news/news.db`.

The page includes source state, successful/failed checks, total articles and
versions, a 96-bin polling timeline, the available 24-hour news set and per-item
acquisition history. A quiet 24-hour window shows the six latest older archived
headlines, explicitly labeled outside the selected window. Latest mode refreshes
every minute; a historical cutoff stays fixed. The source filter applies to news;
archive counts and collection health remain labeled as all sources.

Use the date form (UTC), or `?as_of=2026-11-11T09:00:00Z&source=coindesk`.
Epoch seconds, timezone-aware ISO timestamps and the form's UTC datetime-local
values are accepted. Future, invalid and non-finite cutoffs return HTTP 400.
Allowed filters are `all`, `frb_press`, `bls_cpi`, `bls_empsit`, `bls_jolts` and
`coindesk`. Only the latest version acquired at/before the cutoff is considered,
then publication must satisfy `cutoff - 24h < published_at <= cutoff`.
First/last observations and revision counts are also bounded by the cutoff;
future corrections and late arrivals cannot alter an earlier result.

Paused periods are reconstructed from recorded source configuration changes,
so enabling a previously paused source does not turn its earlier pause into an
outage. The timeline marks observed, unobserved and not-yet-started/paused bins.
Extended gaps exceed 1.5 configured polling periods. They measure observation
coverage, not the unknowable number of missing articles. State reflects the DB,
not a direct systemd health probe. A source can be healthy while publishing no
new articles.

## CoinDesk RSS decision

On 2026-11-12 the operator explicitly instructed that CoinDesk RSS use be treated
as permitted. The live collector's CoinDesk source was enabled with RSS storage,
model-input and public-display flags, and the operator decision recorded in its
policy basis. This does not assert a separate written license. The previous config
was retained privately for rollback. Collection stays on the existing 15-minute
timer and only requests the RSS endpoint; linked article pages are not fetched.

The public text allowlist is FRB, BLS CPI/employment/JOLTS and CoinDesk RSS.
Additional configured feeds appear in collection-health metrics but their text
is not automatically republished. Adding another public text source requires
an explicit review/update of `PUBLIC_HOSTS` and the source query. Headline links
must use HTTPS and the source's domain. All text and attributes are escaped;
RSS/HTML is never rendered as markup. Preserve source attribution.

No raw XML, response blobs, internal source configuration, local paths, credentials
or exception messages are exposed by the HTML or JSON endpoints. Database reads
use `mode=ro`, `query_only=ON`, a single read transaction, a 0.5-second busy timeout
and a two-second SQL work budget. SQLite WAL shared-memory coordination may occur;
no archive rows/schema are changed. News output is capped at 100 entries with an
explicit truncation indicator; at most 1001 latest-version candidates are loaded.
Unavailable, busy, corrupt or incompatible archives return a clear HTTP 503 page.
Page requests do not fetch feeds, call models, import collector code or run jobs.

## Verification and rollout

Run `python -m unittest test_news_views -q` and the full existing test suite in
an isolated Git clone. Tests cover point-in-time revisions/late arrivals, date
boundaries, timezone/form parsing, source filtering, duplicate URLs, source
activation, paused-vs-gap display, HTML injection and URL filtering, private-data
exclusion, read-only DB bytes, unavailable DBs and bounded public lists.

The production implementation uses the existing Flask/Gunicorn service. Changed
runtime files are `app.py`, `ui.py`, `static/style.css` and the new `news_views.py`.
The prediction implementations and Phase 7 frozen sources are not modified.
Before rollout, compare the current production head and source hashes to the
tested base, check `git apply --check` for the exact patch, and keep a rollback
patch. Apply only this change, then gracefully reload the Gunicorn master with
`sudo systemctl kill --kill-whom=main --signal=HUP btc-web.service` (the existing
unit has no ExecReload). Verify the public HTML/JSON paths and existing routes.
News collection continues on its existing timer independently of the web reload.

Rollback the dashboard by reversing only this patch and gracefully reloading
Gunicorn. Keep `/opt/btc-news/news.db` and its collection history. The separate
operator-approved CoinDesk RSS configuration is not reverted by a UI rollback.
