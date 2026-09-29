"""Validate proposals before defaults can erase missing fields or evidence age."""
import math

from lib import now_utc, parse_ts


class CandidateInputError(ValueError):
    """The proposal cannot authorize scoring, delivery or persistence."""


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
