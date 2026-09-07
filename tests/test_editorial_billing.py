import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pipeline
import subscription_runtime as runtime
import utils
import verifier


def test_actual_verifier_passes_image_and_context_through_subscription_client(tmp_path):
    path=tmp_path/'image.png'
    path.write_bytes(b'fixture'*60)
    client=utils.get_anthropic_client('ignored-api-key')
    with mock.patch.object(runtime,'generate',return_value=(json.dumps({'ok':True,'reason':'一致'}),{})) as generate:
        result=verifier.verify_image(client,path,'100から80へ減少',chapter='貿易',block_context='輸出量の比較')
    assert result['ok'] and result['verified']
    assert generate.call_args.kwargs['model']=='gpt-6-astra'
    assert generate.call_args.kwargs['effort']=='high'
    assert generate.call_args.kwargs['workload']=='assets_review'
    assert generate.call_args.kwargs['attachments']
    assert '貿易' in json.dumps(generate.call_args.args[1], ensure_ascii=False)


def test_malformed_review_is_unverified(tmp_path):
    path=tmp_path/'image.png'
    path.write_bytes(b'fixture'*60)
    client=mock.Mock()
    client.messages.create.return_value=SimpleNamespace(content=[SimpleNamespace(text='{"ok":"true"}')])
    result=verifier.verify_image(client,path,'x')
    assert not result['ok'] and not result['verified']


def test_unverified_image_does_not_trigger_paid_regeneration(tmp_path,monkeypatch):
    client=mock.Mock()
    client.with_options.return_value=client
    monkeypatch.setattr(pipeline,'get_anthropic_client',lambda *a:client)
    monkeypatch.setattr(verifier,'verify_image',lambda *a,**k:{'ok':False,'verified':False,'reason':'未確認','fix_hint':''})
    generator=mock.Mock(side_effect=AssertionError('unverified must not regenerate'))
    monkeypatch.setattr(pipeline,'run_parallel_generation',generator)
    pipe=pipeline.SentencePipeline(manuscript_text='fixture',output_dir=tmp_path/'job')
    pipe._verify_and_fix([{'index':1,'success':True}],[{'index':1,'type':'diagram'}],'','')
    assert pipe._rows_state[1]['verify_status']=='unverified'
    generator.assert_not_called()
