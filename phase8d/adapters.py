"""Provider boundaries. No implicit retries, model fallback, browsing or credentials in state."""
import json
import math
import os
import re
import urllib.error
import urllib.request
from .core import canonical, normalize, number, KEYS, PROMPT, CRITERIA

ENDPOINTS = {'gpt6_astra': 'https://api.openai.com/v1/responses',
             'claude_fable': 'https://api.anthropic.com/v1/messages',
             'kairos': 'https://minara.cloud/v1/systemone'}


def redact(value):
    """Keep provider data, replacing credential fields and any known key echoed in text."""
    secrets = [os.environ[k] for k in KEYS.values() if os.environ.get(k)]
    if isinstance(value, dict):
        return {k: '[REDACTED]' if re.search(r'authorization|api.?key|secret|password', k, re.I)
                else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, '[REDACTED]')
        return re.sub(r'(?i)bearer\s+\S+|sk-[a-zA-Z0-9_-]{8,}', '[REDACTED]', value)
    if isinstance(value, float) and not math.isfinite(value):
        return '[NONFINITE]'
    return value


class ProviderError(Exception):
    def __init__(self, code, http_status=None):
        super().__init__(code)
        self.code, self.http_status = code, http_status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProviderError('redirect_refused', code)


def post(engine, payload, timeout):
    headers = {'Content-Type': 'application/json', 'User-Agent': 'btc-phase8d/1'}
    key = os.environ[KEYS[engine]]
    if engine == 'claude_fable':
        headers.update({'x-api-key': key, 'anthropic-version': '2026-10-21'})
    else:
        headers['Authorization'] = 'Bearer ' + key
    req = urllib.request.Request(ENDPOINTS[engine], canonical(payload).encode(), headers, method='POST')
    try:
        # Do not inherit arbitrary proxy settings or forward credentials across redirects.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(req, timeout=timeout) as response:
            body = response.read(1024*1024+1)
            if len(body) > 1024*1024:
                raise ProviderError('response_too_large')
            try:
                return json.loads(body)
            except (ValueError, UnicodeError):
                return {'unparsed_response': body.decode('utf-8', errors='replace')}
    except urllib.error.HTTPError as exc:
        raise ProviderError('http_error', exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ProviderError('network_or_timeout_unknown_charge') from None


def request(engine, state, config):
    model = config['engines'][engine]['model']
    if engine in ('jev', 'kairos'):
        payload = {'model': model, 'state': state,
                   'questions': {'direction': {'type': 'choice', 'instructions': PROMPT, 'criteria': CRITERIA}}}
        if engine == 'kairos':
            payload['task_profile'] = config['kairos_profile']
        return payload
    instruction = PROMPT + ' Return only JSON: {"probability_up": number between 0 and 1}.'
    if engine == 'gpt6_astra':
        return {'model': model, 'instructions': instruction, 'input': canonical(state),
                'max_output_tokens': config['max_output_tokens'], 'reasoning': {'effort': 'low'},
                'store': False, 'service_tier': 'default'}
    return {'model': model, 'system': instruction, 'service_tier': 'standard_only',
            'messages': [{'role': 'user', 'content': canonical(state)}],
            'max_tokens': config['max_output_tokens'], 'output_config': {'effort': 'low'}}


def parse(engine, raw):
    model = raw['model']
    if engine in ('jev', 'kairos'):
        if engine == 'kairos' and raw.get('task_profile', 'general') != 'general':
            raise ValueError('unexpected_task_profile')
        a = raw['answers']['direction']
        probs = a['probabilities']
        if set(probs) != {'UP', 'DOWN'} or any(not 0 <= number(p) <= 1 for p in probs.values()):
            raise ValueError('invalid_distribution')
        if abs(sum(probs.values())-1) > 1e-6:
            raise ValueError('probability_sum')
        if a['choice'] not in probs or probs[a['choice']] < max(probs.values()):
            raise ValueError('choice_mismatch')
        result = normalize(probs['UP'], model, a.get('confidence'))
        result['provider_model_version'] = raw.get('model_version')
        result['provider_calibration_version'] = raw.get('calibration_version')
        return result
    if engine == 'gpt6_astra':
        if raw.get('status') != 'completed':
            raise ValueError('incomplete_response')
        texts = [c['text'] for out in raw['output'] if out.get('type') == 'message'
                 for c in out['content'] if c.get('type') == 'output_text']
    else:
        if raw.get('stop_reason') != 'end_turn':
            raise ValueError('incomplete_response')
        texts = [c['text'] for c in raw['content'] if c['type'] == 'text']
    data = json.loads(''.join(texts))
    if set(data) != {'probability_up'}:
        raise ValueError('unexpected_response_schema')
    return normalize(data['probability_up'], model)


def cost(raw, engine_config):
    """Tariff estimate only; not a claim of settled billing. Unknown usage stays null."""
    usage = raw.get('usage', {})
    try:
        inp, out = number(usage['input_tokens']), number(usage['output_tokens'])
        if min(inp, out) < 0:
            return None
        return (inp*engine_config['input_per_million_usd'] +
                out*engine_config['output_per_million_usd'])/1e6 + engine_config['per_call_usd']
    except (KeyError, TypeError, ValueError):
        return None


def preflight(config, state, *, allow_unpriced_kairos=False):
    if not config['live_enabled']:
        raise ProviderError('live_disabled')
    for engine, settings in config['engines'].items():
        if not os.environ.get(KEYS[engine]):
            raise ProviderError('missing_' + KEYS[engine])
        if not settings['account_verified']:
            raise ProviderError('account_unverified_' + engine)
        bound = len(canonical(request(engine, state, config)).encode()) + 512
        if engine == 'kairos' and bound > 16384:
            raise ProviderError('kairos_pilot_context_limit')
        if engine == 'kairos' and allow_unpriced_kairos:
            continue
        if not settings['pricing_verified']:
            raise ProviderError('account_or_pricing_unverified_' + engine)
        if any(settings[k] is None for k in ('input_per_million_usd', 'output_per_million_usd', 'per_call_usd')):
            raise ProviderError('unknown_price_' + engine)
        # UTF-8 bytes plus envelope allowance is deliberately conservative for text tokenizers.
        bound = len(canonical(request(engine, state, config)).encode()) + 512
        if engine == 'kairos' and bound > 16384:
            raise ProviderError('kairos_pilot_context_limit')
        ceiling = (bound*settings['input_per_million_usd'] +
                   config['max_output_tokens']*settings['output_per_million_usd'])/1e6 + settings['per_call_usd']
        if ceiling > config['reserve_per_call_usd']:
            raise ProviderError('reservation_too_small_' + engine)
    # Import before reserving/sending any of the four calls.
    try:
        from typesafe_sdk import TypeSafeClient, Choice, RetryPolicy  # noqa: F401
    except ImportError:
        raise ProviderError('typesafe_sdk_required') from None


def invoke(engine, state, config):
    payload = request(engine, state, config)
    if engine != 'jev':
        return post(engine, payload, config['timeout_seconds'])
    # Reuse the existing predict_jev.py native TypeSafe interface, with explicit zero retries.
    # Do not import predict_jev: it loads market/experiment dependencies and has a 1h prompt.
    from typesafe_sdk import Choice, TypeSafeClient, RetryPolicy
    client = TypeSafeClient(api_key=os.environ['TYPESAFE_API_KEY'],
                          timeout=config['timeout_seconds'], retry=RetryPolicy(max_retries=0))
    try:
        response = client.system_one(model=payload['model'], state=state,
                                     questions={'direction': Choice(instructions=PROMPT, criteria=CRITERIA)})
        return response.model_dump(mode='json')
    finally:
        close = getattr(client, 'close', None)
        if close:
            close()


def fixture_invoke(engine, state, config):
    """Offline plumbing only: outputs must never be interpreted as forecast performance."""
    p = {'gpt6_astra': .6, 'claude_fable': .55, 'jev': .4, 'kairos': .65}[engine]
    raw = {'model': 'fixture/'+engine, 'usage': {'input_tokens': 0, 'output_tokens': 0}}
    if engine in ('jev', 'kairos'):
        raw['answers'] = {'direction': {'choice': 'UP' if p >= .5 else 'DOWN',
                                      'probabilities': {'UP': p, 'DOWN': 1-p}, 'confidence': .123}}
    elif engine == 'gpt6_astra':
        raw.update(status='completed', output=[{'type': 'message', 'content': [
            {'type': 'output_text', 'text': canonical({'probability_up': p})}]}])
    else:
        raw.update(stop_reason='end_turn', content=[{'type': 'text', 'text': canonical({'probability_up': p})}])
    return raw
