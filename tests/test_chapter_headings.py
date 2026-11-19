"""章題の印のある行は章の区切りにし、画像を作る文（センテンス）に入れない（2026-11-19）。

さゆみさん（11/18）「章タイトルが本文と同じように『1センテンス』として認識され、画像生成の対象に
なっていました。章タイトルの書式や記号などをあらかじめ統一し、その形式の文は画像生成の対象から
自動的に除外するような設定は可能でしょうか？」。
それまでは「3行以上の空行の後・30字以内・。！？で終わらない」行だけを章題にしていたため、
「なぜ〜のか？」型の章題や、原稿スタジオの「【章】見出し」の行が1文として画像の対象になっていた。
"""
from io import BytesIO

from docx import Document

import splitter


def sentences(chapters):
    return [s for row in splitter.flatten_to_rows(chapters) for s in [row["sentence"]]]


def titles(chapters):
    return [c["title"] for c in chapters]


STUDIO = """【導入】円安はなぜ止まらないのでしょうか。
今日はその仕組みを見ます。

【章】なぜ日本だけ金利が低いのか？
日本は長く低い金利を続けてきました。
その理由は二つあります。

【章】
円はどこへ流れるのか
お金は金利の高い国へ動きます。

【締め】
仕組みが分かると、ニュースの見え方が変わります。
"""


def test_studio_markers_become_chapter_titles_not_sentences():
    chapters = splitter.mechanical_split(STUDIO)
    assert titles(chapters) == ["導入", "なぜ日本だけ金利が低いのか？", "円はどこへ流れるのか", "締め"]
    got = sentences(chapters)
    assert "円安はなぜ止まらないのでしょうか。" in got  # 【導入】の後ろの文は本文
    assert not any("【" in s or "なぜ日本だけ金利が低いのか" in s or s == "円はどこへ流れるのか" for s in got)
    assert len(got) == 6


def test_symbol_markdown_and_numbered_headings():
    text = ("## 第1章　ロシアはなぜ冬に強いのか？\n冬の寒さは攻める側を苦しめます。\n\n"
            "■ 補給の線が伸びる\n道は長く、物は届きません。\n\n"
            "第3章　焦土作戦\n町を焼いて退きました。\n\n"
            "【第4章】二度目の冬\n同じことが繰り返されました。\n")
    chapters = splitter.mechanical_split(text)
    assert titles(chapters) == ["第1章　ロシアはなぜ冬に強いのか？", "補給の線が伸びる", "第3章　焦土作戦",
                                "【第4章】二度目の冬"]
    assert sentences(chapters) == ["冬の寒さは攻める側を苦しめます。", "道は長く、物は届きません。",
                                   "町を焼いて退きました。", "同じことが繰り返されました。"]


def test_text_before_the_first_heading_stays_as_an_unnamed_chapter():
    chapters = splitter.mechanical_split("最初の文です。\n■ 一つ目\n本文です。")
    assert titles(chapters) == ["", "一つ目"]
    assert sentences(chapters) == ["最初の文です。", "本文です。"]


def test_heading_without_body_is_dropped():
    chapters = splitter.mechanical_split("# 動画の題\n## 一つ目の章\n本文です。")
    assert titles(chapters) == ["一つ目の章"]


def test_body_lines_that_look_alike_stay_sentences():
    text = ("■ まとめ\n第1章では、金利の差を見てきました。\n●●という会社は値上げしました。\n"
            "【速報】で伝えられたのは、この数字でした。\nなぜでしょうか？\n")
    chapters = splitter.mechanical_split(text)
    assert titles(chapters) == ["まとめ"]
    assert sentences(chapters) == ["第1章では、金利の差を見てきました。", "●●という会社は値上げしました。",
                                   "【速報】で伝えられたのは、この数字でした。", "なぜでしょうか？"]


def test_manuscript_without_markers_is_split_as_before():
    text = "オープニング\n今日の話です。\n大事な話です。\n\n\n大陸の掟\n広い国は守りやすい。"
    chapters = splitter.mechanical_split(text)
    assert titles(chapters) == ["オープニング", "大陸の掟"]
    assert sentences(chapters) == ["今日の話です。", "大事な話です。", "広い国は守りやすい。"]
    # 印の無い問いの行は今までどおり本文（推測で章題にしない）
    plain = splitter.mechanical_split("なぜ日本だけ金利が低いのか？\n日本は低い金利でした。")
    assert "なぜ日本だけ金利が低いのか？" in sentences(plain)


def test_crlf_text_is_read_the_same():
    chapters = splitter.mechanical_split(STUDIO.replace("\n", "\r\n"))
    assert titles(chapters) == ["導入", "なぜ日本だけ金利が低いのか？", "円はどこへ流れるのか", "締め"]


def test_docx_marked_paragraph_in_normal_style_is_a_heading():
    doc = Document()
    doc.add_paragraph("【導入】はじめの文です。")
    doc.add_heading("見出しの書式の章", level=2)
    doc.add_paragraph("一つ目の本文です。")
    doc.add_paragraph("■ 書式の無い章題")
    doc.add_paragraph("二つ目の本文です。")
    buffer = BytesIO()
    doc.save(buffer)
    _, chapters = splitter.parse_docx_to_chapters(buffer.getvalue())
    assert titles(chapters) == ["導入", "見出しの書式の章", "書式の無い章題"]
    assert sentences(chapters) == ["はじめの文です。", "一つ目の本文です。", "二つ目の本文です。"]
