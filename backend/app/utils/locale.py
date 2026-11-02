import json
import os
import re
import threading
from flask import request, has_request_context

_thread_local = threading.local()

_locales_dir = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'locales')

# Load language registry
with open(os.path.join(_locales_dir, 'languages.json'), 'r', encoding='utf-8') as f:
    _languages = json.load(f)

# Load translation files
_translations = {}
for filename in os.listdir(_locales_dir):
    if filename.endswith('.json') and filename != 'languages.json':
        locale_name = filename[:-5]
        with open(os.path.join(_locales_dir, filename), 'r', encoding='utf-8') as f:
            _translations[locale_name] = json.load(f)

_supported_locales = set(_translations)
_quality_re = re.compile(r'^(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)$')


def _negotiate_locale(header):
    if not isinstance(header, str):
        return 'en'

    candidates = []
    for order, item in enumerate(header[:1000].split(',')[:30]):
        parts = item.strip().split(';')
        language_range = parts[0].strip().lower()
        if language_range == '*':
            continue
        if not re.fullmatch(r'[a-z]{1,8}(?:-[a-z0-9]{1,8})*', language_range):
            continue
        quality = 1.0
        valid = True
        for parameter in parts[1:]:
            name, separator, value = parameter.strip().partition('=')
            if not separator or name.strip().lower() != 'q':
                continue
            if not _quality_re.fullmatch(value.strip()):
                valid = False
                break
            quality = float(value.strip())
        if valid and quality > 0:
            candidates.append((quality, order, language_range))

    for _, _, language_range in sorted(candidates, key=lambda item: (-item[0], item[1])):
        if language_range in _supported_locales:
            return language_range
        primary = language_range.split('-', 1)[0]
        if primary in _supported_locales:
            return primary
    return 'en'


def _translation(locale, key):
    value = _translations.get(locale, {})
    for part in key.split('.'):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def set_locale(locale: str):
    """Set locale for current thread. Call at the start of background threads."""
    _thread_local.locale = locale


def get_locale() -> str:
    if has_request_context():
        return _negotiate_locale(request.headers.get('Accept-Language'))
    locale = getattr(_thread_local, 'locale', 'en')
    return locale if locale in _supported_locales else 'en'


def t(key: str, **kwargs) -> str:
    locale = get_locale()
    value = next((found for candidate in (locale, 'en', 'zh')
                  if (found := _translation(candidate, key)) is not None), None)
    if value is None:
        return key

    if kwargs:
        for k, v in kwargs.items():
            value = value.replace(f'{{{k}}}', str(v))

    return value


def get_language_instruction() -> str:
    locale = get_locale()
    lang_config = _languages.get(locale, _languages.get('en', {}))
    return lang_config.get('llmInstruction', 'Please respond in English.')
