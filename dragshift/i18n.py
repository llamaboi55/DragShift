"""Explicit UI localization. Format IDs, source paths and user data stay intact."""
import json,os,re
from functools import lru_cache
from pathlib import Path

_language='en'
_catalog={}
_patterns=[]
_qt_translator=None


def set_language(language):
    global _language,_catalog,_patterns
    if language not in ('en','ru'):
        from PySide6.QtCore import QLocale
        language='ru' if QLocale.system().name().startswith('ru') else 'en'
    _language=language
    _catalog=json.loads((Path(__file__).with_name('assets')/'ru.json').read_text(encoding='utf-8')) if language=='ru' else {}
    _patterns=[]
    for source,target in sorted(_catalog.items(),key=lambda pair:len(re.sub(r'\{\d+\}','',pair[0])),reverse=True):
        if re.search(r'\{\d+\}',source):
            if len(re.sub(r'\{\d+\}','',source).strip())<1:
                continue
            pieces=re.split(r'(\{\d+\})',source)
            expression=''.join('(.+?)' if re.fullmatch(r'\{\d+\}',piece) else re.escape(piece) for piece in pieces)
            fields=[int(piece[1:-1]) for piece in pieces if re.fullmatch(r'\{\d+\}',piece)]
            _patterns.append((re.compile('^'+expression+'$',re.S),target,fields))
    _translate.cache_clear()
    return language


def language():
    return _language


@lru_cache(maxsize=2048)
def _translate(text):
    if text in _catalog:
        return _catalog[text]
    if ';;' in text:
        return ';;'.join(tr(part) for part in text.split(';;'))
    for pattern,target,fields in _patterns:
        match=pattern.fullmatch(text)
        if match:
            values={str(index):tr(value) for index,value in zip(fields,match.groups())}
            return re.sub(r'\{(\d+)\}',lambda item:values.get(item[1],item[0]),target)
    # Composite details and diagnostics keep filenames/codec output verbatim.
    for separator in ('\n',' · '):
        if separator in text:
            return separator.join(tr(piece) for piece in text.split(separator))
    if text.strip()!=text:
        left=len(text)-len(text.lstrip())
        right=len(text.rstrip())
        return text[:left]+tr(text.strip())+text[right:]
    return text


def tr(value):
    if _language=='en' or not isinstance(value,str) or not value:
        return value
    result=_translate(value)
    if result==value and '&' in value and value.replace('&','') in _catalog:
        return _catalog[value.replace('&','')]
    return result


def tr_items(values):
    return [tr(value) for value in values]


def install(app,language='auto'):
    global _qt_translator
    selected=set_language(os.environ.get('DRAGSHIFT_LANGUAGE',language))
    if _qt_translator:
        app.removeTranslator(_qt_translator)
    if selected=='ru':
        from PySide6.QtCore import QLibraryInfo,QTranslator
        _qt_translator=QTranslator(app)
        bundled=Path(__file__).with_name('assets')/'qtbase_ru.qm'
        loaded=_qt_translator.load(str(bundled)) if bundled.exists() else _qt_translator.load('qtbase_ru',QLibraryInfo.path(QLibraryInfo.TranslationsPath))
        if loaded:
            app.installTranslator(_qt_translator)
    app.setProperty('ui_language',selected)
