"""RSS/Atom adapter and explicit per-feed storage policy (no page scraping)."""
import hashlib
import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

MAX_BYTES = 4 * 1024 * 1024


def digest(value):
    return hashlib.sha256(value).hexdigest()


def timestamp(value):
    """Reject timezone ambiguity rather than silently assuming local time."""
    if not value:
        return None
    try:
        iso = value.strip().replace('Z', '+00:00')
        # Python 3.9 accepts 3/6 fractional digits only; BLS Atom uses 1/2 too.
        iso = re.sub(r'\.(\d+)(?=[+-]\d{2}:\d{2}$)',
                     lambda m: '.'+m[1][:6].ljust(6, '0'), iso)
        d = datetime.fromisoformat(iso)
    except ValueError:
        try:
            d = parsedate_to_datetime(value.strip())
        except (ValueError, TypeError, OverflowError):
            return None
    if d.tzinfo is None:
        return None
    try:
        return int(d.astimezone(timezone.utc).timestamp())
    except (ValueError, OverflowError):
        return None


def canonical_url(value):
    p = urlsplit(value.strip())
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('invalid_url')
    # Never persist credentials from a URL. Keep semantic query parameters.
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in ('fbclid', 'gclid')]
    if any(re.search(r'token|secret|password|api.?key|authorization', k, re.I) for k, _ in query):
        raise ValueError('credential_url')
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or '/', urlencode(sorted(query)), ''))


def text(value):
    return unescape(re.sub(r'<[^>]*>', ' ', value or '')).strip()


def load_config(path):
    with open(path, encoding='utf-8') as f:
        config = json.load(f)
    if config.get('interval_seconds') not in range(600, 901):
        raise ValueError('interval_must_be_600_to_900')
    ids = set()
    for source in config['feeds']:
        if not re.fullmatch(r'[a-z0-9_-]+', source['id']) or source['id'] in ids:
            raise ValueError('invalid_or_duplicate_feed_id')
        ids.add(source['id'])
        if source.get('adapter') != 'rss' or source.get('language') != 'en':
            raise ValueError('unsupported_adapter_or_language')
        url = canonical_url(source['url'])
        if not url.startswith('https://') or urlsplit(url).query:
            raise ValueError('public_https_feed_required')
        source['url'] = url
        policy = source['policy']
        if type(source.get('enabled')) is not bool or type(policy.get('model_input')) is not bool:
            raise ValueError('boolean_policy_flags_required')
        if not all(isinstance(source.get(k), str) and source[k] for k in ('publisher', 'attribution')):
            raise ValueError('source_metadata_required')
        if policy['mode'] not in ('blocked', 'rss'):
            raise ValueError('invalid_storage_policy')
        if policy['mode'] == 'rss' and not policy.get('basis'):
            raise ValueError('permission_basis_required')
        if source.get('enabled') and policy['mode'] == 'blocked':
            raise ValueError('blocked_feed_enabled')
    return config


def parse_rss(body, source):
    """Return normalized entries, item XML and rejection counts. Raw feed kept by store.

    No content:encoded or linked pages are used as model text. A future API adapter
    should return this same (entries, rejected) contract and preserve its raw reply.
    """
    if len(body) > MAX_BYTES or re.search(br'<!\s*(DOCTYPE|ENTITY)', body, re.I):
        raise ValueError('unsafe_or_oversize_xml')
    # Also reject declarations in UTF-16/32; only UTF-8/ASCII feeds are supported.
    if b'\x00' in body:
        raise ValueError('unsupported_xml_encoding')
    root = ET.fromstring(body)
    local = lambda tag: tag.rsplit('}', 1)[-1]
    if local(root.tag) not in ('rss', 'feed', 'RDF'):
        raise ValueError('not_a_feed')
    nodes = [e for e in root.iter() if local(e.tag) in ('item', 'entry')]
    entries, rejected = [], 0
    for node in nodes:
        fields = {}
        for child in node:
            key = local(child.tag)
            if key == 'link' and child.get('href'):
                if child.get('rel', 'alternate') == 'alternate':
                    fields['link'] = child.get('href')
            else:
                fields[key] = ''.join(child.itertext()).strip()
        try:
            url = canonical_url(fields.get('link', ''))
            title = text(fields.get('title'))
            if not title:
                raise ValueError('missing_title')
            # Keep only RSS title/description, never HTML article bodies.
            item = dict(url=url, title=title, summary=text(fields.get('description', fields.get('summary'))),
                        published_at=timestamp(fields.get('pubDate', fields.get('published', fields.get('date')))),
                        publisher_updated_at=timestamp(fields.get('updated')),
                        language=source['language'], attribution=source['attribution'])
            raw = ET.tostring(node, encoding='unicode')
            entries.append(dict(item=item, guid=fields.get('guid', fields.get('id', '')),
                                raw=raw, content_hash=digest(json.dumps(item, sort_keys=True).encode()),
                                raw_hash=digest(raw.encode())))
        except (ValueError, TypeError):
            rejected += 1
    if not nodes:
        # Legitimate empty feeds are allowed, but explicitly observable.
        return [], 0
    return entries, rejected
