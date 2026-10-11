import asyncio
from io import BytesIO
import json

from PIL import Image, ImageDraw, PngImagePlugin
import pytest

import generator
import prompter
import suasi_presenter as presenter
from suasi_pose import select_pose, POSE_NAMES


@pytest.mark.parametrize('sentence,expected', [
    ('実際に稼げた利回りが、約束した予定利率を下回ると逆ざやです。', 'pointing'),
    ('保険を選ぶとき、何を比べればよいのでしょうか？', 'thinking'),
    ('予想に反して、利用者は増えていました。', 'surprised'),
    ('まとめると、仕組みを理解して条件を比べることが大切です。', 'explaining'),
    ('死亡事故が意外に多くなっています。', 'pointing'),
    ('保険会社の損失は1兆円に達しました。', 'pointing'),
    ('なぜ起きるかは、次の仕組みで説明できます。', 'pointing'),
])
def test_pose_matches_source_meaning(sentence, expected):
    assert select_pose({'excerpt':sentence})['pose_id'] == expected


def test_style_boilerplate_is_not_used_to_infer_emotion():
    assert select_pose({'prompt':'surprisingly thinking question? in summary',
                        'excerpt':'会社は保険料を運用します。'})['pose_id'] == 'pointing'
    assert select_pose({'excerpt':'会社は保険料を運用します。','suasi_pose':['bad']})['pose_id'] == 'pointing'
    assert select_pose({'excerpt':'津波で命を失った人を数えます。','suasi_pose':'surprised'})['pose_id'] == 'pointing'


@pytest.mark.parametrize('pose', list(POSE_NAMES))
def test_all_sprites_are_pinned_and_small_beside_larger_diagram(tmp_path, pose):
    path=tmp_path/(pose+'.png')
    board=Image.new('RGB',(1536,864),(255,251,242))
    ImageDraw.Draw(board).rectangle((154,130,1382,734),fill='#90bad7',outline='#001c21',width=8)
    board.save(path)
    recipe=presenter.compose_presenter(path,presenter.load_artwork(pose),pose,'fixture')
    x1,y1,x2,y2=recipe['diagram_box']
    cx1,cy1,cx2,cy2=recipe['character_box']
    assert x2 < cx1 and y1 >= 0 and y2 <= 864
    assert x2-x1 > 1536*.79  # Previously the entire original board was shrunk to 79%.
    assert cx2-cx1 <= round(1536*.115) and cy2-cy1 <= round(864*.31)
    assert 0 < cx1 < cx2 < 1536 and 0 < cy1 < cy2 < 864
    assert presenter.read_recipe(path.read_bytes())['pose_id'] == pose
    with Image.open(BytesIO(presenter.diagram_edit_source(path.read_bytes()))) as edit:
        assert edit.size == (x2-x1,y2-y1)
        assert presenter.RECIPE_KEY not in edit.info


def test_near_edge_text_marks_and_coloured_background_are_not_trimmed():
    image=Image.new('RGB',(500,300),(255,251,242))
    draw=ImageDraw.Draw(image)
    draw.rectangle((50,50,450,250),outline='black',width=5)
    draw.line((2,7,14,7),fill='black',width=2)
    _,box=presenter.trim_diagram(image)
    assert box[0] == 0 and box[1] == 0  # preserve the small edge label as well as main diagram
    blue=Image.new('RGB',(500,300),'blue')
    assert presenter.trim_diagram(blue)[1] == (0,0,500,300)


def test_v2_saved_image_still_edits_only_diagram(tmp_path):
    image=Image.new('RGB',(1024,576),'blue')
    ImageDraw.Draw(image).rectangle((900,300,1000,570),fill='red')
    recipe={'version':2,'art_sha256':presenter.ART_SHA256,
            'diagram_box':[0,60,809,515], 'character_box':[900,300,1000,570]}
    meta=PngImagePlugin.PngInfo()
    meta.add_text(presenter.RECIPE_KEY,json.dumps(recipe))
    path=tmp_path/'v2.png'
    image.save(path,pnginfo=meta)
    with Image.open(BytesIO(presenter.diagram_edit_source(path.read_bytes()))) as edit:
        assert edit.getextrema() == ((0,0),(0,0),(255,255))


def test_edit_preserves_pose_but_explicit_override_can_change_it(tmp_path,monkeypatch):
    source=tmp_path/'source.png'
    Image.new('RGB',(1024,576),'white').save(source)
    presenter.compose_presenter(source,presenter.load_artwork('thinking'),'thinking','fixture')
    gen=generator.ParallelImageGenerator(provider='gpt-image',openai_api_key='test-only',
        style_lock_text='[OTONA_SUASI_B_V1]',edit_image_path=str(source))
    def dispatch(prompt,path,*args):
        Image.new('RGB',(1024,576),'white').save(path)
        return True,''
    monkeypatch.setattr(gen,'_dispatch_sync_generate',dispatch)
    rows=[{'index':1,'type':'diagram','prompt':'Compare conditions','excerpt':'保険の条件を比べます。',
           'edit_source':True},
          {'index':2,'type':'diagram','prompt':'Compare conditions','excerpt':'保険の条件を比べます。',
           'edit_source':True,'suasi_pose':'explaining','suasi_pose_override':True}]
    result=asyncio.run(gen.generate_all(rows,tmp_path/'edited'))
    assert all(r['success'] for r in result)
    assert [r['presenter']['pose_id'] for r in result] == ['thinking','explaining']


def test_missing_pose_blocks_before_paid_client_initialisation(tmp_path,monkeypatch):
    original=presenter.pose_catalog()
    monkeypatch.setattr(presenter,'pose_catalog',lambda:original)
    monkeypatch.setattr(presenter,'POSE_DIR',tmp_path)
    with pytest.raises(ValueError,match='承認済み原画'):
        generator.ParallelImageGenerator(provider='gpt-image',openai_api_key='test-only',
                                         style_lock_text='[OTONA_SUASI_B_V1]')


def test_planner_pose_survives_merging_and_regular_style_is_unaffected(monkeypatch):
    def batch(*args,**kwargs):
        return [{'no':1,'type':'diagram','prompt':'Show the mechanism','suasi_pose':'explaining',
                 'suasi_pose_reason':'全体の関係を整理する'}]
    monkeypatch.setattr(prompter,'generate_prompts_batch',batch)
    rows=[{'no':1,'sentence':'会社が保険料を運用します。','route':'diagram'}]
    b=prompter.generate_all_prompts(None,rows,'fixture',worldview_desc='[OTONA_SUASI_B_V1]')
    assert b[0]['suasi_pose'] == 'explaining'
    plain=prompter.generate_all_prompts(None,rows,'fixture',worldview_desc='')
    assert 'suasi_pose' not in plain[0]
