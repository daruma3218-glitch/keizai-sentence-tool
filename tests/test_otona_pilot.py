"""The new channel reaches the existing workflow without changing other channels."""
import json
from html.parser import HTMLParser
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as appmod
import material_store as store


def test_otona_profile_and_no_notification_destination():
    profile = appmod.get_channel('otona')
    assert profile['id'] == 'otona'
    defaults = profile['defaults']
    assert defaults['max_diagrams'] == 6
    assert defaults['concurrency'] == 2
    assert defaults['allow_ai_realphoto'] is False
    assert not defaults.get('chatwork_room_id')
    assert store.channel('otona')['source_id'] == 'otona'
    assert '参照がない時は似たキャラを新作しない' in defaults['user_instructions']


def test_registry_profiles_resolve_exactly():
    profiles = appmod.load_channels()
    ids = [p['id'] for p in profiles]
    assert len(ids) == len(set(ids))
    for channel in store.CHANNELS.values():
        assert appmod.get_channel(channel['source_id'])['id'] == channel['source_id']


def test_otona_uses_only_approved_material_key(monkeypatch):
    for name in ('OPENAI_API_KEY', 'GEMINI_API_KEY', 'ANTHROPIC_API_KEY'):
        monkeypatch.setenv(name, 'unrelated-common-test-key')
        monkeypatch.delenv('OTONA_' + name, raising=False)
    profile = appmod.get_channel('otona')
    assert appmod.resolve_channel_keys(profile) == {'openai': '', 'gemini': '', 'anthropic': ''}
    monkeypatch.setenv('OTONA_OPENAI_API_KEY', 'approved-tv-material-test-key')
    assert appmod.resolve_channel_keys(profile) == {
        'openai': 'approved-tv-material-test-key', 'gemini': '', 'anthropic': ''}


def test_browser_can_represent_each_channels_diagram_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, 'OUTPUT_ROOT', tmp_path)
    monkeypatch.setattr(appmod, 'OUTPUT_DIR', tmp_path/'output')
    monkeypatch.setattr(appmod, 'APP_PASSWORD', '')
    monkeypatch.setattr(appmod, 'resolve_channel_keys', lambda c: {'openai': 'test-only', 'gemini': '', 'anthropic': ''})

    class Slider(HTMLParser):
        attributes = None

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == 'input' and attrs.get('name') == 'max_diagrams':
                self.attributes = attrs

    slider = Slider()
    slider.feed(appmod.app.test_client().get('/?channel_id=otona').get_data(as_text=True))
    assert slider.attributes, 'Rendered upload form must include the diagram limit'
    low, high, step = (int(slider.attributes[k]) for k in ('min', 'max', 'step'))
    for channel in appmod.load_channels():
        limit = channel['defaults'].get('max_diagrams', 150)
        assert low <= limit <= high and (limit-low) % step == 0, channel['id']


def test_authenticated_entry_and_channel_isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, 'OUTPUT_ROOT', tmp_path)
    monkeypatch.setattr(appmod, 'OUTPUT_DIR', tmp_path/'output')
    monkeypatch.setattr(appmod, 'APP_PASSWORD', 'test-only')
    appmod.app.config.update(TESTING=True)
    client = appmod.app.test_client()
    assert client.get('/materials?channel=otona').status_code == 302
    with client.session_transaction() as session:
        session['authenticated'] = True
        session['material_csrf'] = 'test-only'
    response = client.get('/materials?channel=otona')
    assert response.status_code == 200
    assert '大人の学び直しTV' in response.get_data(as_text=True)
    p = store.create_project(tmp_path, 'otona', '合成試験', {})
    assert p['source_channel_id'] == 'otona'
    assert p['channel_id'] == 'otona'
