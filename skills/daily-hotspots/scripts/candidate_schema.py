"""Validate proposals before defaults can erase missing fields or evidence age."""
import math
import unicodedata

from lib import now_utc, parse_ts, load_config, _handle_origin


class CandidateInputError(ValueError):
    """The proposal cannot authorize scoring, delivery or persistence."""


_PLATFORM_ALIASES = {
    'twitterapi': 'x.com', 'x': 'x.com', 'twitter': 'x.com',
    'x-roster': 'x.com', 'x-broad': 'x.com', 'twitter.com': 'x.com',
    'hackernews': 'news.ycombinator.com', 'hn': 'news.ycombinator.com',
    'product-hunt': 'producthunt.com', 'producthunt': 'producthunt.com',
    'github': 'github.com', 'official-github': 'github.com',
    'reddit': 'reddit.com', 'arxiv': 'arxiv.org',
}
_DEFAULT_MAX_ORIGINS_PER_PLATFORM = 2


def canonical_origin(value):
    """One identity for whitespace, case, scheme and known lane aliases."""
    if not isinstance(value, str):
        raise CandidateInputError('origin must be a nonempty string')
    value = unicodedata.normalize('NFKC', value).strip().casefold()
    if not value or any(unicodedata.category(c).startswith('C') for c in value):
        raise CandidateInputError('origin must be a nonempty plain string')
    for scheme in ('https://', 'http://'):
        if value.startswith(scheme):
            value = value[len(scheme):]
            break
    host, sep, path = value.rstrip('/').partition('/')
    host = host.removeprefix('www.').rstrip('.')
    host = _PLATFORM_ALIASES.get(host, host)
    if not host:
        raise CandidateInputError('origin has no identity')
    return host + (sep + path if path else '')


def _ev_origin(item):
    return canonical_origin(item.get('origin') or item.get('source'))


def normalized_evidence(evidence):
    return [dict(item, origin=_ev_origin(item), source=item['source'].strip()) for item in evidence]


def _distinct_origins(evidence):
    return sorted({_ev_origin(item) for item in evidence if isinstance(item, dict)
                   and (item.get('origin') or item.get('source'))})


def _quote_parent_origin(item):
    handle = item.get('via_handle')
    return canonical_origin(_handle_origin(handle)) if isinstance(handle, str) and handle.strip() else None


def _platform_of(origin):
    return canonical_origin(origin).split('/', 1)[0]


def cfg_max_origins_per_platform(cfg=None):
    value = (cfg or load_config())['scoring'].get('max_origins_per_platform', _DEFAULT_MAX_ORIGINS_PER_PLATFORM)
    try:
        limit = int(value)
    except (TypeError, ValueError, OverflowError):
        return _DEFAULT_MAX_ORIGINS_PER_PLATFORM
    if isinstance(value, bool) or limit < 0 or (isinstance(value, float) and value != limit):
        return _DEFAULT_MAX_ORIGINS_PER_PLATFORM
    return limit


def count_independent_sources(evidence, cfg=None):
    origins = set(_distinct_origins(evidence))
    for origin in list(origins):
        items = [item for item in evidence if _ev_origin(item) == origin]
        if items and all((parent := _quote_parent_origin(item)) is not None
                         and parent != origin and parent in origins for item in items):
            origins.discard(origin)
    cap = cfg_max_origins_per_platform(cfg)
    platforms = {}
    for origin in origins:
        platform = _platform_of(origin)
        platforms[platform] = platforms.get(platform, 0) + 1
    count = sum(min(value, cap) for value in platforms.values()) if cap else len(origins)
    urls = [item.get('url', '').strip().casefold() for item in evidence]
    return min(count, len(set(urls))) if urls and all(urls) else count


def finite_number(value, name, lower=None, upper=None):
    if isinstance(value, bool):
        raise CandidateInputError(name + ' must be a finite number')
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CandidateInputError(name + ' must be a finite number') from exc
    if not math.isfinite(number) or (lower is not None and number < lower) or (upper is not None and number > upper):
        raise CandidateInputError(name + ' is nonfinite or outside its domain')
    return number


def candidate_list(payload):
    if isinstance(payload, dict):
        if 'candidates' not in payload or any(key in payload for key in ('error', 'errors')):
            raise CandidateInputError('candidate envelope is missing candidates or contains an error')
        if (payload.get('ok') is False or payload.get('success') is False
                or payload.get('isError') is True
                or str(payload.get('status', '')).lower() in {'error', 'failed', 'failure'}):
            raise CandidateInputError('candidate envelope reports failure')
        payload = payload['candidates']
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise CandidateInputError('candidates must be a list of objects')
    return payload


def evidence_age(evidence):
    if not isinstance(evidence, dict):
        raise CandidateInputError('evidence must contain objects')
    timestamp = evidence.get('ts')
    if not isinstance(timestamp, str) or not timestamp.strip():
        raise CandidateInputError('evidence timestamp is missing')
    try:
        age = (now_utc() - parse_ts(timestamp)).total_seconds() / 3600
    except (TypeError, ValueError, OverflowError) as exc:
        raise CandidateInputError('evidence timestamp is invalid') from exc
    if age < -5 / 60:
        raise CandidateInputError('evidence timestamp is in the future')
    return max(0.0, age)


def field_errors(card, cfg, *, allow_unclassified=False):
    if not isinstance(card, dict):
        return ['candidate must be an object']
    errors = []
    track = card.get('track')
    tracks = {row['id'] for row in cfg['tracks'] if row.get('enabled', True)}
    if not (allow_unclassified and not track) and (not isinstance(track, str) or track not in tracks):
        errors.append('track must name an enabled configured track')
    side = card.get('side', 'supply')
    if side not in ('supply', 'demand'):
        errors.append('side must be supply or demand')
    prose = ['title', 'summary', 'why_now', 'contrarian_insight', 'action']
    if side == 'demand':
        prose.append('pain_evidence')
    for name in prose:
        if not isinstance(card.get(name), str) or not card[name].strip():
            errors.append('missing or invalid ' + name)
    for name in ('entities', 'machine_type', 'focus_tags'):
        if name in card and (not isinstance(card[name], list)
                             or any(not isinstance(value, str) for value in card[name])):
            errors.append(name + ' must be a list of strings')
    evidence = card.get('evidence')
    if not isinstance(evidence, list) or not evidence:
        errors.append('evidence must be a nonempty list')
    else:
        for index, item in enumerate(evidence):
            try:
                evidence_age(item)
            except CandidateInputError as exc:
                errors.append(f'evidence[{index}]: {exc}')
            if not isinstance(item, dict) or any(
                    not isinstance(item.get(key), str) or not item[key].strip()
                    for key in ('url', 'source')):
                errors.append(f'evidence[{index}] requires url and source')
            elif isinstance(item, dict):
                try:
                    _ev_origin(item)
                    if 'origin' in item:
                        canonical_origin(item['origin'])
                except CandidateInputError as exc:
                    errors.append(f'evidence[{index}]: {exc}')
    for name, lower, upper in (('velocity', -1, 1), ('crowdedness', 0, 100)):
        if card.get(name) is not None:
            try:
                finite_number(card[name], name, lower, upper)
            except CandidateInputError as exc:
                errors.append(str(exc))
    return errors


def validated_age(candidate, cfg):
    errors = field_errors(candidate, cfg, allow_unclassified=True)
    if errors:
        raise CandidateInputError('; '.join(errors))
    # The newest evidence still gives a lower bound on the proposal's age. An
    # explicit upstream age may make it older, never turn old evidence into news.
    age = min(evidence_age(item) for item in candidate['evidence'])
    if 'age_hours' in candidate:
        try:
            supplied = float(candidate['age_hours'])
        except (TypeError, ValueError, OverflowError) as exc:
            raise CandidateInputError('age_hours must be finite and nonnegative') from exc
        if not math.isfinite(supplied) or supplied < 0:
            raise CandidateInputError('age_hours must be finite and nonnegative')
        age = max(age, supplied)
    return age
