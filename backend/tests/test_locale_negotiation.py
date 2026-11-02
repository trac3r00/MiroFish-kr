"""Seeded property tests for Accept-Language negotiation."""
import random

import pytest
from flask import Flask

from app.utils import locale as locale_utils


SUPPORTED = locale_utils._supported_locales


def oracle(header):
    """Independent reference model for supported exact/primary matching."""
    if not isinstance(header, str):
        return 'en'
    ranked = []
    for index, item in enumerate(header[:1000].split(',')[:30]):
        parts = item.strip().split(';')
        tag = parts[0].strip().lower()
        if tag == '*' or not tag:
            continue
        quality = 1.0
        for parameter in parts[1:]:
            name, separator, value = parameter.strip().partition('=')
            if separator and name.strip().lower() == 'q':
                try:
                    quality = float(value.strip())
                except ValueError:
                    quality = -1
                if not 0 <= quality <= 1 or len(value.strip().split('.')[-1]) > 3:
                    quality = -1
                break
        if quality > 0:
            ranked.append((-quality, index, tag))
    for _, _, tag in sorted(ranked):
        primary = tag.split('-', 1)[0]
        if tag in SUPPORTED:
            return tag
        if primary in SUPPORTED:
            return primary
    return 'en'


ORACLE_EXAMPLES = [
    ('ko-KR,ko;q=0.9,en;q=0.8', 'ko'),
    ('en-US', 'en'),
    ('zh-CN', 'zh'),
    ('zh-Hant-TW', 'zh'),
    ('fr;q=0.1, ko;q=0.5', 'ko'),
    ('de', 'en'),
    ('*', 'en'),
    ('', 'en'),
    (None, 'en'),
    ('ko;q=0,zh;q=0.5', 'zh'),
]


@pytest.mark.parametrize(('header', 'expected'), ORACLE_EXAMPLES)
def test_spec_example_negotiation(header, expected):
    app = Flask(__name__)
    with app.test_request_context(headers={'Accept-Language': header} if header is not None else {}):
        expected = expected if expected in SUPPORTED else oracle(header)
        assert locale_utils.get_locale() == expected


def test_invalid_and_oversized_headers_fall_back_without_raising():
    for header in (b'\xff\xfe', 'a' * 1001, 'x,' * 40, 'en;q=bogus'):
        assert locale_utils._negotiate_locale(header) == 'en'


def test_seeded_property_generated_headers_stay_supported():
    rng = random.Random(9110)
    tags = ['en', 'en-US', 'zh', 'zh-Hant-TW', 'fr', 'ko-KR', 'de', '*', 'garbage']
    qualities = ['', ';q=0', ';q=0.2', ';q=0.8', ';q=1', ';q=bogus']
    attempted = 2500
    for _ in range(attempted):
        header = ','.join(
            rng.choice(tags) + rng.choice(qualities)
            for _ in range(rng.randint(0, 8))
        )
        result = locale_utils._negotiate_locale(header)
        assert result in SUPPORTED
        assert result == oracle(header)
    print(f'property tests: attempted={attempted}, executed={attempted}')


def test_translation_fallback_chain_requested_then_english_then_chinese(monkeypatch):
    monkeypatch.setattr(locale_utils, '_translations', {
        'ko': {'present': '요청 언어', 'en_only': 'unused'},
        'en': {'english_only': 'English'},
        'zh': {'chinese_only': '中文'},
    })
    monkeypatch.setattr(locale_utils, 'get_locale', lambda: 'ko')
    assert locale_utils.t('present') == '요청 언어'
    assert locale_utils.t('english_only') == 'English'
    assert locale_utils.t('chinese_only') == '中文'
    assert locale_utils.t('missing') == 'missing'
