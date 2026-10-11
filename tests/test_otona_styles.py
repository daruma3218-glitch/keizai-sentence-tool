"""Selectable TV styles must survive saved-project, fallback and resume paths."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys

from PIL import Image
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as appmod
import generator
import material_store as store
import prompter
from channel_styles import resolve_style, style_choices, is_suasi_style


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, 'OUTPUT_ROOT', tmp_path)
    monkeypatch.setattr(appmod, 'OUTPUT_DIR', tmp_path/'output')
    monkeypatch.setattr(appmod, 'APP_PASSWORD', '')
    monkeypatch.setattr(appmod, 'resolve_channel_keys', lambda c: {'openai':'test-only','gemini':'','anthropic':''})
    appmod.app.config['TESTING'] = True
    client = appmod.app.test_client()
    with client.session_transaction() as session:
        session['material_csrf'] = 'test-only'
    return client


def create(client, style='suasi_explainer', **extra):
    return client.post('/api/material-projects', json={
        'channel_id':'otona', 'title':'テイスト確認', 'style_id':style, **extra},
        headers={'X-Material-CSRF':'test-only'})


def test_style_choice_saved_once_and_other_channels_reject_it(client):
    page = client.get('/materials?channel=otona').get_data(as_text=True)
    assert 'いつものTV素材' in page and 'すあし社長の図解解説（B）' in page
    result = create(client, trello_url='https://trello.com/c/abcdefgh').get_json()['project']
    assert is_suasi_style(result['profile']['worldview_desc'])
    assert result['profile']['character_ref'].endswith('otona-suasi.jpg')
    assert 'キャラの常設はしない' not in result['profile']['user_instructions']
    assert '通常の図解は' not in result['profile']['user_instructions']
    again = create(client, 'tv_standard', trello_url='https://trello.com/c/abcdefgh').get_json()['project']
    assert again['profile_sha256'] == result['profile_sha256']
    assert create(client, channel_id='economy').status_code == 409
    assert create(client, 'unknown').status_code == 409
    normal = create(client, 'tv_standard').get_json()['project']['profile']
    assert normal['character_ref'] == '' and normal['chart_engine'] == 'render'


def test_style_resolution_does_not_mutate_channel():
    channel = appmod.get_channel('otona')
    frozen = json.dumps(channel, sort_keys=True)
    b = resolve_style(channel, 'suasi_explainer')
    b['chart_theme']['main'] = 'changed'
    assert json.dumps(channel, sort_keys=True) == frozen
    assert len(style_choices(channel)) == 2


def test_suasi_uses_approved_bytes_for_diagram_even_without_character_flag(tmp_path, monkeypatch):
    d = resolve_style(appmod.get_channel('otona'), 'suasi_explainer')
    ref = appmod.PROJECT_ROOT / d['character_ref']
    assert hashlib.sha256(ref.read_bytes()).hexdigest() == 'bd91548b76670fb61604dbef2bd7bd2f4011001cec52c0ff75f3e0b7a2197d33'
    gen = generator.ParallelImageGenerator(provider='gpt-image', openai_api_key='test-only',
        reference_image_path=str(ref), style_lock_text=d['worldview_desc'])
    seen = []
    def dispatch(prompt, path, use_reference=False, ref_bytes_override=None, ref_mime_override=None):
        seen.append((prompt, use_reference))
        Image.new('RGB', (64,36), 'white').save(path)
        return True, ''
    monkeypatch.setattr(gen, '_dispatch_sync_generate', dispatch)
    result = asyncio.run(gen.generate_all([
        {'index':1,'type':'diagram','prompt':'Income < promised payment.','character':False}], tmp_path))
    assert result[0]['success'], result
    prompt, has_ref = seen[0]
    assert has_ref and gen.reference_bytes == ref.read_bytes()
    assert 'APPROVED SUASI SHACHO' in prompt
    for forbidden in ('professor', 'gray tweed', 'half-lidded', 'PRESET STYLE'):
        assert forbidden not in prompt
    assert '12-18%' in prompt


def test_suasi_missing_reference_fails_before_api(tmp_path):
    d = resolve_style(appmod.get_channel('otona'), 'suasi_explainer')
    with pytest.raises(ValueError, match='承認済み参照画像'):
        generator.ParallelImageGenerator(provider='gpt-image', openai_api_key='test-only',
            reference_image_path=str(tmp_path/'missing.jpg'), style_lock_text=d['worldview_desc'])


def test_prompter_does_not_import_other_channels_presenter():
    c = appmod.get_channel('otona')
    for style in style_choices(c):
        block = prompter._style_lock_block(resolve_style(c, style['id'])['worldview_desc'])
        assert 'THE PROFESSOR' not in block
        assert '場合がある' in block
    assert '固定キャラの常設はしない' in prompter._style_lock_block(c['defaults']['worldview_desc'])


def test_start_and_resume_keep_selected_style_with_stale_form(client, tmp_path, monkeypatch):
    p = create(client).get_json()['project']
    class NoThread:
        def __init__(self, **kwargs): pass
        def start(self): pass
    monkeypatch.setattr(appmod.threading, 'Thread', NoThread)
    r = client.post('/start', data={'channel_id':'otona','project_id':p['project_id'],
        'provider':'gpt-image','manuscript_text':'保険会社が預かった保険料を運用します。'*12,
        'worldview_mode':'off','worldview_desc':'stale other look','max_diagrams':'2'})
    assert r.status_code == 200, r.get_data(as_text=True)
    job_id = r.get_json()['job_id']
    d = tmp_path/'output'/job_id
    saved = store.read(d/'material_context.json')
    assert is_suasi_style(saved['generation_settings']['worldview_desc'])
    assert appmod._job_defaults(d, appmod.get_channel('otona'))['style_id'] == 'suasi_explainer'
    args, error = appmod._build_resume_args(job_id)
    assert error is None
    assert is_suasi_style(args[11]) and args[15].endswith('otona-suasi.jpg')
    old = dict(p['profile'], style_id='', worldview_mode=False, worldview_desc='', style_lock=False, character_ref='')
    saved.update(profile=old, generation_settings={})
    store.save(d/'material_context.json', saved)
    assert appmod._job_defaults(d, appmod.get_channel('otona'))['style_lock'] is False


def test_legacy_regeneration_gets_b_profile_and_reference(client, tmp_path, monkeypatch):
    p = create(client).get_json()['project']
    job_id = '20261011_000000_b'
    d = tmp_path/'output'/job_id
    d.mkdir(parents=True)
    store.bind_job(tmp_path, d, p)
    store.save(d/'job.json', {'channel_id':'otona','provider':'gpt-image','worldview_desc':p['profile']['worldview_desc'],'style_lock':True})
    row = {'no':1,'route':'diagram','type':'diagram','prompt':'運用利回りと予定利率を比較','sentence':'運用利回りが予定利率を下回る。'}
    store.save(d/'prompts.json', {'rows':[row]})
    store.save(d/'rows_progress.json', {'rows':[row]})
    seen = {}
    def generate(**kwargs):
        seen.update(kwargs)
        return [{'success':False,'index':1,'error':'synthetic stop'}]
    monkeypatch.setattr(generator, 'run_parallel_generation', generate)
    client.post('/api/regenerate/'+job_id+'/1')
    assert is_suasi_style(seen['style_lock_text'])
    assert seen['reference_image_path'].endswith('otona-suasi.jpg')
