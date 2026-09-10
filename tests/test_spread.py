"""Offline tests for cross-venue matching. Run: .venv/bin/python -m unittest tests.test_spread"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prediction_markets.main import _matches, _spread_monitor_eligible  # noqa: E402
from prediction_markets.spread import (auto_pairs, bracket_signature, build_pairs, date_keys, direction_keys, match_events,
                                       pair_row, parse_pairs, semantic_compatible, tokens, year_keys)  # noqa: E402


def k(id_, event, label, title=None, p=0.5, bid=None, ask=None):
    return {'source': 'kalshi', 'id': id_, 'event_id': event.split('|')[0], 'event_title': event.split('|')[1], 'series_title': None,
            'title': title or label, 'outcome_label': label, 'yes_price': p, 'yes_bid': bid, 'yes_ask': ask, 'status': 'open',
            'close_time': '2026-09-09T05:00:00Z', 'url': 'https://kalshi.com/markets/x', 'event_url': 'https://kalshi.com/markets/x/y/z'}


def pm(id_, event, label, title=None, p=0.5, bid=None, ask=None, slug='slug'):
    return {'source': 'polymarket', 'id': id_, 'event_id': event.split('|')[0], 'event_title': event.split('|')[1],
            'title': title or label, 'outcome_label': label, 'yes_price': p, 'yes_bid': bid, 'yes_ask': ask, 'status': 'open',
            'close_time': '2026-09-08T12:00:00Z', 'url': f'https://polymarket.com/event/e/{slug}'}


class Signatures(unittest.TestCase):
    def test_brackets_agree_across_venues(self):
        self.assertEqual(bracket_signature('82° to 83°'), bracket_signature('82-83°F'))
        self.assertEqual(bracket_signature('79° or below'), bracket_signature('79°F or below'))
        self.assertEqual(bracket_signature('80° or above'), bracket_signature('80°F or higher'))
        self.assertNotEqual(bracket_signature('79° or below'), bracket_signature('69°F or below'))

    def test_non_brackets(self):
        self.assertIsNone(bracket_signature('Trump'))
        self.assertIsNone(bracket_signature('1 (25 bps)'))
        self.assertEqual(bracket_signature('↑ 5.25%'), ('eq', 5.25))
        self.assertEqual(bracket_signature('Hike >25bps'), ('ge', 25.0))

    def test_tokens_and_dates(self):
        a = 'Highest temperature in New York City on Sep 8, 2026?'
        b = 'Highest temperature in NYC on September 8?'
        self.assertEqual(tokens(a), tokens(b))
        self.assertEqual(date_keys(a), date_keys(b))
        self.assertNotEqual(date_keys(a), date_keys('Highest temperature in NYC on September 9?'))
        self.assertEqual(year_keys(a), frozenset({2026}))
        self.assertFalse(semantic_compatible('Fed hike September 2027', 'Fed increase September 2026'))
        self.assertFalse(semantic_compatible('Hike 25bps', 'Decrease 25 bps'))
        self.assertEqual(direction_keys('Fed raises rates'), frozenset({'up'}))


class Matching(unittest.TestCase):
    def test_weather_event_pairs_by_bracket(self):
        ke = 'KXHIGHNY-26SEP08|Highest temperature in New York City on Sep 8, 2026?'
        pe = '123|Highest temperature in NYC on September 8?'
        ks = [k('K-T80', ke, '80° to 81°', p=0.65), k('K-T82', ke, '82° to 83°', p=0.05), k('K-LOW', ke, '79° or below', p=0.30)]
        ps = [pm('P82', pe, '82-83°F', p=0.02), pm('P80', pe, '80-81°F', p=0.72), pm('PLOW', pe, '69°F or below', p=0.0)]
        pairs = auto_pairs(ks, ps, 0.6)
        got = {(a['id'], b['id']) for a, b, _ in pairs}
        self.assertEqual(got, {('K-T80', 'P80'), ('K-T82', 'P82')})  # tails differ (79 vs 69): not paired

    def test_date_conflict_blocks_pair(self):
        ks = [k('K1', 'E1|Highest temperature in NYC on Sep 8, 2026?', '80° to 81°')]
        ps = [pm('P1', 'E2|Highest temperature in NYC on September 9?', '80-81°F')]
        self.assertEqual(auto_pairs(ks, ps, 0.6), [])

    def test_unrelated_events_do_not_pair(self):
        ks = [k('K1', 'E1|Fed decision in Sep 2026?', 'Cut 25bps')]
        ps = [pm('P1', 'E2|How many Fed rate cuts in 2026?', '1 (25 bps)')]
        self.assertEqual(auto_pairs(ks, ps, 0.6), [])

    def test_single_market_events_pair_by_title(self):
        ks = [k('K1', 'E1|Will Elon Musk visit Mars in his lifetime?', None, title='Will Elon Musk visit Mars in his lifetime?')]
        ps = [pm('P1', 'E2|Elon Musk visits Mars in his lifetime?', None, title='Elon Musk visits Mars in his lifetime?')]
        pairs = auto_pairs(ks, ps, 0.6)
        self.assertEqual(len(pairs), 1)

    def test_year_and_direction_false_matches_are_rejected(self):
        ks = [k('KXFED-27SEP-H25', 'E1|Fed decision in Sep', 'Hike 25bps',
                title='Will the Federal Reserve hike rates by 25bps at its September 2027 meeting?')]
        ps = [pm('P1', 'E2|Fed decision in September', '25 bps decrease',
                 title='Will the Fed decrease interest rates by 25 bps after the September 2026 meeting?')]
        self.assertEqual(auto_pairs(ks, ps, 0.6), [])

    def test_single_market_scope_mismatch_is_rejected(self):
        ks = [k('K1', 'E1|What will Trump say during RNC Convention Night 1?', 'Rigged Election',
                title='What will Donald Trump say during RNC Convention Night 1?')]
        ps = [pm('P1', 'E2|What will Trump say during RNC Convention Night 1?', 'Crypto / Bitcoin',
                 title='Will Trump say Crypto or Bitcoin during RNC Convention Night 1?')]
        self.assertEqual(auto_pairs(ks, ps, 0.6), [])

    def test_market_score_cannot_fall_below_threshold(self):
        ks = [k('K1', 'E1|Where will it rain on Sep 9, 2026?', 'New York City')]
        ps = [pm('P1', 'E2|Where will it rain on Sep 9, 2026?', 'New York')]
        self.assertEqual(auto_pairs(ks, ps, 0.8), [])

    def test_explicit_pairs_win_and_missing_reported(self):
        ks = [k('KX-A', 'E1|Anything', 'x')]
        ps = [pm('0xabc', 'E2|Other', 'y', slug='my-slug')]
        rows, stats = build_pairs(ks, ps, parse_pairs(['kx-a=my-slug', 'KX-B=nope']), 0.6)
        self.assertEqual(stats, {'explicit': 1, 'auto': 0, 'missing': ['KX-B=nope']})
        self.assertEqual(rows[0]['match_method'], 'explicit')
        self.assertEqual(rows[0]['id'], 'KX-A|0xabc')


class Economics(unittest.TestCase):
    def test_edge_and_fee(self):
        # Kalshi YES ask 0.30, Polymarket YES bid 0.40: buy YES on Kalshi, NO on Polymarket, edge 10 pts before fees.
        row = pair_row(k('K', 'E|t', 'x', p=0.29, bid=0.28, ask=0.30), pm('P', 'E|t', 'x', p=0.41, bid=0.40, ask=0.42), 1.0, 'auto')
        self.assertEqual(row['spread_pts'], -12.0)
        self.assertEqual(row['arb_edge_pts'], 10.0)
        self.assertEqual(row['arb_direction'], 'yes_kalshi_no_polymarket')
        self.assertEqual(row['kalshi_fee_est_pts'], 1.47)  # ceil(7*0.3*0.7*100)/100
        self.assertEqual(row['net_edge_pts'], 8.53)

    def test_no_edge_when_books_overlap(self):
        row = pair_row(k('K', 'E|t', 'x', p=0.5, bid=0.49, ask=0.51), pm('P', 'E|t', 'x', p=0.5, bid=0.49, ask=0.51), 1.0, 'auto')
        self.assertIsNone(row['arb_direction'])
        self.assertLess(row['arb_edge_pts'], 0)

    def test_missing_prices(self):
        row = pair_row(k('K', 'E|t', 'x', p=None), pm('P', 'E|t', 'x', p=0.5), 1.0, 'auto')
        self.assertIsNone(row['spread_pts'])
        self.assertIsNone(row['arb_edge_pts'])

    def test_keyword_search_does_not_match_opaque_ids(self):
        rec = {'id': '0x123fed456', 'series_id': 'KXFED', 'title': 'Will it rain?', 'event_title': 'Rain today',
               'outcome_label': 'Yes', 'series_title': 'Weather'}
        self.assertFalse(_matches(rec, ['fed']))
        self.assertFalse(_matches({**rec, 'id': 'safe', 'title': 'Hunter Feduccia: 1+ home runs?'}, ['fed']))
        self.assertTrue(_matches({**rec, 'id': 'safe', 'title': 'Will the Fed cut rates?'}, ['fed']))

    def test_spread_monitor_quality_gate(self):
        now = __import__('datetime').datetime(2026, 9, 9, tzinfo=__import__('datetime').timezone.utc)
        good = {'match_score': 0.9, 'net_edge_pts': 2, 'arb_direction': 'yes_kalshi_no_polymarket',
                'kalshi_close_time': '2026-09-10T00:00:00Z', 'polymarket_close_time': '2026-09-10T00:00:00Z'}
        self.assertTrue(_spread_monitor_eligible(good, 0.6, now))
        self.assertFalse(_spread_monitor_eligible({**good, 'net_edge_pts': -1}, 0.6, now))
        self.assertFalse(_spread_monitor_eligible({**good, 'match_score': 0.5}, 0.6, now))
        self.assertFalse(_spread_monitor_eligible({**good, 'polymarket_close_time': '2026-09-08T00:00:00Z'}, 0.6, now))


if __name__ == '__main__':
    unittest.main()
