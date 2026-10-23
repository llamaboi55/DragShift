import json,re
from pathlib import Path
import pytest
from PySide6.QtWidgets import QLabel,QComboBox
from dragshift import i18n
from dragshift.media_controls import EffectEnvelope
from dragshift.settings import Settings,SettingsWindow


@pytest.fixture(autouse=True)
def restore_language():
    yield
    i18n.set_language('en')


def test_russian_labels_templates_diagnostics_and_stable_identifiers(qt_session):
    i18n.set_language('ru')
    assert i18n.tr('Cancel')=='Отмена'
    assert i18n.tr('Save MP4')=='Сохранить MP4'
    assert i18n.tr('25 fps')=='25 кадр/с'
    assert i18n.tr('Video · Resize')=='Видео · Размер'
    assert i18n.tr('Output: 160 × 96 px')=='Результат: 160 × 96 пикс.'
    assert i18n.tr('2 files  ·  516.9 KB')=='Файлов: 2  ·  516.9 КБ'
    message='Video processing failed. Try a different format or smaller resolution.\n[libx264] raw diagnostic'
    assert i18n.tr(message).startswith('Не удалось обработать видео.')
    assert i18n.tr(message).endswith('[libx264] raw diagnostic')
    for code in ('MP4','GIF','WEBM','PNG','WAV','Melt','tool:Resize','photo-Settings.png','C:/User/first.mp4'):
        assert i18n.tr(code)==code
    assert i18n.tr('Audio (*.wav *.flac);;All files (*)')=='Звук (*.wav *.flac);;Все файлы (*)'


def test_actual_russian_controls_preserve_envelope_data(qt_session):
    i18n.install(qt_session,'ru')
    envelope=EffectEnvelope()
    assert envelope.shape.itemText(0)=='Равномерно'
    assert envelope.shape.currentData()=='constant'
    envelope.shape.setCurrentIndex(1)
    assert envelope.shape.currentData()=='build' and envelope.points()==[(0,0),(1,1)]
    assert 'В начале чисто' in envelope.hint.text()
    envelope.close()


def test_settings_language_has_persisted_values_and_source_catalog(qt_session):
    window=SettingsWindow(Settings())
    assert {window.language.itemData(n) for n in range(window.language.count())}=={'auto','ru','en'}
    path=Path(i18n.__file__).with_name('assets')/'ru.json'
    data=json.loads(path.read_text(encoding='utf-8'))
    assert len(data)>600 and all(isinstance(k,str) and isinstance(v,str) and v for k,v in data.items())
    window.close()
