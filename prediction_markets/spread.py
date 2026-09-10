"""Cross-venue spread mode: pair the same question on Kalshi and Polymarket and price the gap.

Matching is two-level and deliberately conservative: an event on Kalshi is paired with an event on
Polymarket only when their titles agree after normalization while explicit years, dates, and action
directions do not conflict; markets inside a matched event pair are paired by bracket signature
("80-81", "79 or below", "80 or higher"), by single-market events, or by outcome-label overlap.
Anything the matcher is not sure about is left unpaired; explicit `pairs` input always wins.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import datetime, timezone

MONTHS = {'jan': 'january', 'feb': 'february', 'mar': 'march', 'apr': 'april', 'may': 'may', 'jun': 'june', 'jul': 'july',
          'aug': 'august', 'sep': 'september', 'sept': 'september', 'oct': 'october', 'nov': 'november', 'dec': 'december'}
MONTH_NUM = {m: i + 1 for i, m in enumerate(['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
                                              'september', 'october', 'november', 'december'])}
# Applied to lowercase text, longest phrase first, before tokenizing.
ALIASES = [
    ('new york city', 'nyc'), ('new york', 'nyc'), ('los angeles', 'la'), ('washington d c', 'dc'), ('washington dc', 'dc'),
    ('san francisco', 'sf'), ('federal reserve', 'fed'), ('united states', 'us'), ('basis points', 'bps'),
    ('maximum temperature', 'highest temperature'), ('max temperature', 'highest temperature'), ('high temperature', 'highest temperature'),
    ('minimum temperature', 'lowest temperature'), ('min temperature', 'lowest temperature'), ('low temperature', 'lowest temperature'),
    ('bitcoin', 'btc'), ('ethereum', 'eth'), ('degrees', ''), ('deg', ''),
]
STOP = {'will', 'the', 'be', 'on', 'in', 'of', 'a', 'an', 'to', 'at', 'by', 'for', 'and', 'or', 'is', 'than', 'what', 'who',
        'does', 'do', 'this', 'that', 'it', 'its', 'with', 'their', 'there', 'between', 'f', 'question', 'market'}
_YEAR = re.compile(r'^(19|20)\d\d$')
_YEAR_IN_TEXT = re.compile(r'\b(?:19|20)\d{2}\b')
_NUM = re.compile(r'-?\d+(?:\.\d+)?')
_ORD = re.compile(r'\b(\d+)(st|nd|rd|th)\b')
_DATE_MD = re.compile(r'\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})\b')
_DATE_DM = re.compile(r'\b(\d{1,2})\s+(january|february|march|april|may|june|july|august|september|october|november|december)\b')

# A pair can have identical titles and brackets while settling against different
# facts.  Keep the gate intentionally conservative: a six-hour difference is
# already too large for a row advertised as an executable cross-venue edge.
MAX_CLOSE_TIME_DELTA_HOURS = 6.0

# Canonicalize only authorities that the rules name unambiguously.  Do not turn
# generic phrases such as "official results" into a match: two venues can both
# say "official" while relying on different agencies or data vendors.
_AUTHORITY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ('the_weather_company', re.compile(r'\bthe weather company\b|\bweather\.com\b', re.I)),
    ('noaa', re.compile(r'\bnoaa\b|\bnational oceanic and atmospheric administration\b|\bnational weather service\b|\bweather\.gov\b', re.I)),
    ('bls', re.compile(r'\bbureau of labor statistics\b|\bbls\.gov\b|\bBLS\b', re.I)),
    ('bea', re.compile(r'\bbureau of economic analysis\b|\bbea\.gov\b|\bBEA\b', re.I)),
    ('federal_reserve', re.compile(r'\bfederal reserve\b|\bfederalreserve\.gov\b', re.I)),
    ('cme', re.compile(r'\bchicago mercantile exchange\b|\bcme group\b|\bcmegroup\.com\b|\bCME\b', re.I)),
    ('fec', re.compile(r'\bfederal election commission\b|\bfec\.gov\b|\bFEC\b', re.I)),
    ('associated_press', re.compile(r'\bassociated press\b|\bAP News\b', re.I)),
    ('reuters', re.compile(r'\breuters\b', re.I)),
    ('cnn', re.compile(r'\bcnn\b', re.I)),
    ('fox_news', re.compile(r'\bfox news\b', re.I)),
)


def clean(text: str | None) -> str:
    t = (text or '').lower().replace('’', "'").replace('–', '-').replace('—', '-')
    t = re.sub(r'°\s*f?\b', ' ', t)
    t = re.sub(r'°', ' ', t)
    t = _ORD.sub(r'\1', t)
    t = re.sub(r"[^a-z0-9.\-%$ ]+", ' ', t)
    t = re.sub(r'(?<=\d)-(?=\d)', ' ', t)  # 80-81 -> 80 81
    t = re.sub(r'[%$]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    words = [MONTHS.get(w, w) for w in t.split()]
    t = ' ' + ' '.join(words) + ' '
    for a, b in ALIASES:
        t = t.replace(f' {a} ', f' {b} ')
    return re.sub(r'\s+', ' ', t).strip()


def tokens(text: str | None) -> frozenset[str]:
    out = set()
    for w in clean(text).split():
        w = w.strip('.-')
        if not w or w in STOP or _YEAR.match(w):
            continue
        out.add(w)
    return frozenset(out)


def date_keys(text: str | None) -> frozenset[tuple[int, int]]:
    t = clean(text)
    keys = {(MONTH_NUM[m], int(d)) for m, d in _DATE_MD.findall(t)}
    keys |= {(MONTH_NUM[m], int(d)) for d, m in _DATE_DM.findall(t)}
    return frozenset(k for k in keys if 1 <= k[1] <= 31)


def year_keys(text: str | None) -> frozenset[int]:
    """Explicit resolution years are hard semantic constraints, not stop words."""
    return frozenset(int(y) for y in _YEAR_IN_TEXT.findall(text or ''))


def direction_keys(text: str | None) -> frozenset[str]:
    """Monetary/directional meaning that a numeric bracket alone loses."""
    t = clean(text)
    out = set()
    if re.search(r'\b(hike|hikes|hiked|increase|increases|increased|raise|raises|raised)\b', t):
        out.add('up')
    if re.search(r'\b(cut|cuts|decrease|decreases|decreased|reduce|reduces|reduced)\b', t):
        out.add('down')
    if re.search(r'\b(no change|unchanged|hold|holds|held)\b', t):
        out.add('flat')
    return frozenset(out)


def semantic_compatible(a: str | None, b: str | None) -> bool:
    """Reject contracts with explicit conflicting years or action directions."""
    ay, by = year_keys(a), year_keys(b)
    if ay and by and not (ay & by):
        return False
    ad, bd = direction_keys(a), direction_keys(b)
    if ad and bd and not (ad & bd):
        return False
    return True


def settlement_authorities(rec: dict) -> tuple[str, ...]:
    """Return canonical authorities explicitly named in a contract's rules."""
    text = str(rec.get('rules') or '')
    return tuple(name for name, pattern in _AUTHORITY_PATTERNS if pattern.search(text))


def _utc_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def settlement_compatibility(k: dict, p: dict) -> dict:
    """Machine-check the minimum evidence needed before publishing an edge.

    ``compatible`` means both contracts name at least one common settlement
    authority and their stated close times are within six hours. ``incompatible``
    means an explicit authority or close-time conflict. Missing evidence is
    ``unverified``. Only ``compatible`` rows may expose arb/net edge fields.
    """
    ka, pa = settlement_authorities(k), settlement_authorities(p)
    reasons: list[str] = []
    incompatible = False

    authority_verified = bool(ka and pa and set(ka) & set(pa))
    if ka and pa and not authority_verified:
        incompatible = True
        reasons.append(f"settlement_authority_mismatch:{','.join(ka)}!={','.join(pa)}")
    elif not authority_verified:
        reasons.append('settlement_authority_unverified')

    kt, pt = _utc_time(k.get('close_time')), _utc_time(p.get('close_time'))
    delta = round(abs((kt - pt).total_seconds()) / 3600, 3) if kt and pt else None
    close_verified = delta is not None and delta <= MAX_CLOSE_TIME_DELTA_HOURS
    if delta is not None and not close_verified:
        incompatible = True
        reasons.append(f'close_time_delta_exceeds_{MAX_CLOSE_TIME_DELTA_HOURS:g}h')
    elif delta is None:
        reasons.append('close_time_unverified')

    if incompatible:
        status = 'incompatible'
        compatible: bool | None = False
    elif authority_verified and close_verified:
        status = 'compatible'
        compatible = True
    else:
        status = 'unverified'
        compatible = None
    return {
        'settlement_compatible': compatible,
        'settlement_status': status,
        'settlement_reasons': reasons,
        'kalshi_settlement_authorities': list(ka),
        'polymarket_settlement_authorities': list(pa),
        'close_time_delta_hours': delta,
    }


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def bracket_signature(label: str | None) -> tuple | None:
    """('range', lo, hi) | ('le', n) | ('ge', n) | ('eq', n) | None, from an outcome label or market title."""
    t = clean(label)
    if not t:
        return None
    nums = [float(x) for x in _NUM.findall(t)]
    nums = [n for n in nums if not (n.is_integer() and 1900 <= n <= 2100)]  # drop years
    raw = (label or '').lower().replace('–', '-')
    is_range = re.search(r'\d\s*(?:-|to)\s*\d', re.sub(r'°\s*f?', '', raw)) is not None
    if len(nums) >= 2 and is_range and not re.search(r'\b(below|above|higher|lower|less|more|under|over)\b', t):
        return ('range', min(nums[:2]), max(nums[:2]))
    if nums and (re.search(r'\b(or below|or less|or lower|or under|below|under|less than|at most)\b', t) or '<' in raw):
        return ('le', nums[0])
    if nums and (re.search(r'\b(or above|or higher|or more|or over|above|over|more than|at least|higher)\b', t) or '>' in raw):
        return ('ge', nums[0])
    if len(nums) == 1 and len(t.split()) <= 4:
        return ('eq', nums[0])
    return None


def _event_text(rec: dict) -> str:
    return rec.get('event_title') or rec.get('series_title') or rec.get('title') or ''


def match_events(k_events: dict[str, dict], p_events: dict[str, dict], min_score: float) -> list[tuple[str, str, float]]:
    """Return (kalshi_event_id, polymarket_event_id, score) for the best mutual matches above `min_score`.

    `*_events` map event id -> {'text': str, 'markets': [rec, ...]}.
    """
    def semantic_text(e: dict) -> str:
        parts = [e.get('text') or '']
        for r in e.get('markets') or []:
            parts.extend((r.get('event_title') or '', r.get('title') or '', r.get('outcome_label') or ''))
        return ' '.join(parts)

    p_sem = {pid: semantic_text(e) for pid, e in p_events.items()}
    p_tok = {pid: tokens(e['text']) for pid, e in p_events.items()}
    p_date = {pid: date_keys(e['text']) for pid, e in p_events.items()}
    index: dict[str, set[str]] = defaultdict(set)
    for pid, toks in p_tok.items():
        for w in toks:
            index[w].add(pid)
    scored: list[tuple[float, str, str]] = []
    for kid, e in k_events.items():
        kt = tokens(e['text'])
        kd = date_keys(e['text'])
        k_sem = semantic_text(e)
        cands: dict[str, int] = defaultdict(int)
        for w in kt:
            for pid in index.get(w, ()):
                cands[pid] += 1
        need = 1 if len(kt) <= 2 else 2
        for pid, hits in cands.items():
            if hits < need:
                continue
            if kd and p_date[pid] and not (kd & p_date[pid]):
                continue
            if not semantic_compatible(k_sem, p_sem[pid]):
                continue
            s = jaccard(kt, p_tok[pid])
            if s >= min_score:
                scored.append((s, kid, pid))
    scored.sort(reverse=True)
    used_k: set[str] = set()
    used_p: set[str] = set()
    out = []
    for s, kid, pid in scored:
        if kid in used_k or pid in used_p:
            continue
        used_k.add(kid)
        used_p.add(pid)
        out.append((kid, pid, round(s, 3)))
    return out


def match_markets(k_rows: list[dict], p_rows: list[dict]) -> list[tuple[dict, dict, float]]:
    """Pair markets inside one matched event pair. Signature match first, then 1:1 events, then label overlap."""
    def contract_text(r: dict) -> str:
        return ' '.join(str(r.get(x) or '') for x in ('event_title', 'title', 'outcome_label'))

    def labels_compatible(k: dict, p: dict) -> bool:
        if not semantic_compatible(contract_text(k), contract_text(p)):
            return False
        kl, pl = tokens(k.get('outcome_label')), tokens(p.get('outcome_label'))
        generic = {'yes', 'no'}
        if kl and pl and not (kl <= generic or pl <= generic):
            return jaccard(kl, pl) >= 0.5
        return True

    if len(k_rows) == 1 and len(p_rows) == 1:
        kr, pr = k_rows[0], p_rows[0]
        if not labels_compatible(kr, pr):
            return []
        ks, ps = bracket_signature(kr.get('outcome_label') or kr.get('title')), bracket_signature(pr.get('outcome_label') or pr.get('title'))
        if ks is not None or ps is not None:
            return [(kr, pr, 1.0)] if ks == ps else []
        score = jaccard(tokens(contract_text(kr)), tokens(contract_text(pr)))
        return [(kr, pr, round(score, 3))] if score >= 0.5 else []
    out = []
    used_p: set[int] = set()
    k_sig = [(bracket_signature(r.get('outcome_label') or r.get('title')), r) for r in k_rows]
    p_sig = [(bracket_signature(r.get('outcome_label') or r.get('title')), r) for r in p_rows]
    for ks, kr in k_sig:
        if ks is None:
            continue
        for j, (ps, pr) in enumerate(p_sig):
            if j in used_p or ps is None:
                continue
            if labels_compatible(kr, pr) and ks[0] == ps[0] and all(math.isclose(a, b, abs_tol=1e-6) for a, b in zip(ks[1:], ps[1:])):
                used_p.add(j)
                out.append((kr, pr, 1.0))
                break
    matched_k = {id(kr) for kr, _, _ in out}
    scored = []
    for ks, kr in k_sig:
        if id(kr) in matched_k or ks is not None:
            continue
        kt = tokens(kr.get('outcome_label') or kr.get('title'))
        for j, (ps, pr) in enumerate(p_sig):
            if j in used_p or ps is not None:
                continue
            if not labels_compatible(kr, pr):
                continue
            s = jaccard(kt, tokens(pr.get('outcome_label') or pr.get('title')))
            if s >= 0.5:
                scored.append((s, kr, j, pr))
    scored.sort(key=lambda x: -x[0])
    for s, kr, j, pr in scored:
        if j in used_p or id(kr) in matched_k:
            continue
        used_p.add(j)
        matched_k.add(id(kr))
        out.append((kr, pr, round(s, 3)))
    return out


def auto_pairs(k_rows: list[dict], p_rows: list[dict], min_score: float) -> list[tuple[dict, dict, float]]:
    k_events: dict[str, dict] = {}
    for r in k_rows:
        eid = r.get('event_id') or r['id']
        k_events.setdefault(eid, {'text': _event_text(r), 'markets': []})['markets'].append(r)
    p_events: dict[str, dict] = {}
    for r in p_rows:
        eid = r.get('event_id') or r['id']
        p_events.setdefault(eid, {'text': _event_text(r), 'markets': []})['markets'].append(r)
    out = []
    for kid, pid, score in match_events(k_events, p_events, min_score):
        for kr, pr, ms in match_markets(k_events[kid]['markets'], p_events[pid]['markets']):
            final = round(min(score, ms), 3)
            # Explicit conflicts are not candidates. Unknown authorities may
            # still be useful as price-divergence rows, but pair_row suppresses
            # every edge field until compatibility is positively verified.
            terms = settlement_compatibility(kr, pr)
            if final >= min_score and terms['settlement_status'] != 'incompatible':
                out.append((kr, pr, final))
    return out


def _pts(x: float | None) -> float | None:
    return None if x is None else round(x * 100, 2)


def pair_row(k: dict, p: dict, score: float, method: str) -> dict:
    kp, pp = k.get('yes_price'), p.get('yes_price')
    spread = None if kp is None or pp is None else round((kp - pp) * 100, 2)
    terms = settlement_compatibility(k, p)
    # Executable edge before fees: buy YES where it is cheaper (at the ask), buy NO on the other venue
    # (which costs 1 - that venue's YES bid). Both legs pay $1 together on settlement, so edge = bid - ask.
    edges: list[tuple[float, str, float]] = []
    if k.get('yes_ask') is not None and p.get('yes_bid') is not None and k['yes_ask'] > 0:
        edges.append((round((p['yes_bid'] - k['yes_ask']) * 100, 2), 'yes_kalshi_no_polymarket', k['yes_ask']))
    if p.get('yes_ask') is not None and k.get('yes_bid') is not None and p['yes_ask'] > 0:
        edges.append((round((k['yes_bid'] - p['yes_ask']) * 100, 2), 'yes_polymarket_no_kalshi', 1 - k['yes_bid']))
    edge, direction, k_leg = max(edges) if edges and terms['settlement_compatible'] is True else (None, None, None)
    # Kalshi taker fee: 7% x P x (1-P) per contract, rounded up to the cent. Polymarket has no taker fee on most markets.
    fee = None if k_leg is None else round(math.ceil(7 * k_leg * (1 - k_leg) * 100) / 100, 2)
    net = None if edge is None else round(edge - fee, 2)
    closes = [t for t in (k.get('close_time'), p.get('close_time')) if t]
    return {
        'source': 'spread',
        'id': f"{k['id']}|{p['id']}",
        'title': k.get('title'),
        'outcome_label': k.get('outcome_label') or p.get('outcome_label'),
        'event_title': k.get('event_title') or p.get('event_title'),
        'match_score': score,
        'match_method': method,
        **terms,
        'spread_pts': spread,
        'abs_spread_pts': None if spread is None else abs(spread),
        'arb_edge_pts': edge,
        'arb_direction': direction if (edge is not None and edge > 0) else None,
        'kalshi_fee_est_pts': fee,
        'net_edge_pts': net,
        'kalshi_id': k['id'],
        'kalshi_title': k.get('title'),
        'kalshi_outcome': k.get('outcome_label'),
        'kalshi_url': k.get('event_url') or k.get('url'),
        'kalshi_yes_bid': k.get('yes_bid'),
        'kalshi_yes_ask': k.get('yes_ask'),
        'kalshi_yes_price': kp,
        'kalshi_volume_24h': k.get('volume_24h'),
        'kalshi_open_interest': k.get('open_interest'),
        'kalshi_liquidity': k.get('liquidity'),
        'kalshi_close_time': k.get('close_time'),
        'kalshi_settlement_station': k.get('settlement_station'),
        'kalshi_rules': (k.get('rules') or '')[:400] or None,
        'polymarket_id': p['id'],
        'polymarket_title': p.get('title'),
        'polymarket_outcome': p.get('outcome_label'),
        'polymarket_url': p.get('url'),
        'polymarket_yes_bid': p.get('yes_bid'),
        'polymarket_yes_ask': p.get('yes_ask'),
        'polymarket_yes_price': pp,
        'polymarket_volume_24h': p.get('volume_24h'),
        'polymarket_liquidity': p.get('liquidity'),
        'polymarket_yes_token_id': p.get('yes_token_id'),
        'polymarket_close_time': p.get('close_time'),
        'polymarket_rules': (p.get('rules') or '')[:400] or None,
        'close_time': min(closes) if closes else None,
        'status': 'open' if k.get('status') == 'open' and p.get('status') == 'open' else (k.get('status') or p.get('status')),
        'fetched_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
    }


def parse_pairs(items: list[str]) -> list[tuple[str, str]]:
    """'KXHIGHNY-26SEP08-T80=will-the-highest-...' -> [('KXHIGHNY-26SEP08-T80', 'will-the-highest-...')]."""
    out = []
    for s in items or []:
        s = str(s).strip()
        if not s:
            continue
        sep = '=' if '=' in s else ('|' if '|' in s else None)
        if not sep:
            continue
        left, right = s.split(sep, 1)
        left, right = left.strip().upper(), right.strip()
        right = right.rsplit('/', 1)[-1] if right.startswith('http') else right
        if left and right:
            out.append((left, right if right.startswith('0x') else right.lower()))
    return out


def build_pairs(k_rows: list[dict], p_rows: list[dict], explicit: list[tuple[str, str]], min_score: float) -> tuple[list[dict], dict]:
    """Explicit pairs first (by Kalshi ticker and Polymarket slug/condition id), then auto matching on the rest."""
    k_by_id = {r['id'].upper(): r for r in k_rows}
    p_by_key: dict[str, dict] = {}
    for r in p_rows:
        p_by_key[str(r['id']).lower()] = r
        slug = (r.get('url') or '').rsplit('/', 1)[-1].lower()
        if slug:
            p_by_key[slug] = r
    rows = []
    used_k, used_p = set(), set()
    missing = []
    for kt, pk in explicit:
        k, p = k_by_id.get(kt), p_by_key.get(pk.lower())
        if k is None or p is None:
            missing.append(f'{kt}={pk}')
            continue
        rows.append(pair_row(k, p, 1.0, 'explicit'))
        used_k.add(k['id'])
        used_p.add(p['id'])
    rest_k = [r for r in k_rows if r['id'] not in used_k]
    rest_p = [r for r in p_rows if r['id'] not in used_p]
    auto = auto_pairs(rest_k, rest_p, min_score)
    rows.extend(pair_row(k, p, s, 'auto') for k, p, s in auto)
    return rows, {'explicit': len(rows) - len(auto), 'auto': len(auto), 'missing': missing}
