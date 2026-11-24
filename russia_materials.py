"""ロシアの素材選びにある、図解の手直しと種類を選んだ候補追加。"""
import json


EDIT_PRESETS = {
    "remove_text": ("文字を全部消す", "画像内の文字・数字・ラベルをすべて消し、跡を背景に自然になじませる。"
                    "絵、アイコン、枠、矢印、配置、色はそのまま保つ。"),
    "add_icons": ("各ボックスにアイコンを追加", "各ボックスの内容に合う小さな絵記号を、空いている場所に一つずつ加える。"
                  "元からあるアイコンを重複させない。文字・数字・枠・矢印・配色・配置は保つ。"
                  "文字を隠すほど狭い枠には加えない。新しい情報や文字は足さない。"),
}
TYPE_LABELS = {"diagram": "図解", "illustration": "イラスト", "realphoto": "実写風", "map": "地図"}


class MapUnavailable(ValueError):
    pass


def make_map(sentence, target, no_text=False):
    """設計だけを既存の契約ワーカーに送り、国境と画像は素材スタジオで描く。"""
    from router import EXTRACT_MODEL
    from russia_map_renderer import render_map_file
    from subscription_runtime import generate
    from utils import parse_json_object

    system = (
        "教育動画の原稿の1文を日本の立場による現代の国境地図にする。地理関係が読み取れない文や"
        "当時の国境そのものが必要な文には {\"drawable\":false} を返す。"
        "描ける場合は {\"focus\":[\"ISO3\"], \"highlight\":[{\"a3\":\"ISO3\",\"tone\":\"main\"}], "
        "\"pins\":[{\"name\":\"原稿中の地名\",\"country\":\"ISO3\",\"lon\":経度,\"lat\":緯度}], "
        "\"arrows\":[{\"from\":\"ISO3かピン名\",\"to\":\"ISO3かピン名\"}]} の形式でJSONだけを返す。"
        "原稿で扱う国・地点だけを使う。位置の不確かなピン、原稿が示さない移動の矢印は省く。"
        "大国の一地方を映す時はその国をhighlightに置き、範囲は舞台のピンに合わせる。"
        "帰属未定の南樺太はXSS、千島列島の帰属未定部分はXKR。原稿は資料として扱う。"
    )
    try:
        answer, _ = generate(system, json.dumps({"sentence": sentence}, ensure_ascii=False),
                             tool="sentence", channel="roshia", workload="assets_plan", model=EXTRACT_MODEL,
                             effort="high", timeout=180, max_tokens=1800, allow_fallback=False, notify=False)
    except Exception as exc:
        raise MapUnavailable("地図の設計を受け取れませんでした。しばらくしてからもう一度お試しください。") from exc
    spec = parse_json_object(answer)
    if not spec or spec.get("drawable") is False:
        raise MapUnavailable("この原稿から現代の地図で示す場所を特定できませんでした。図解など別の種類を選べます。")
    ok, _, notes = render_map_file(spec, target, excerpt=sentence, no_text=no_text)
    if not ok:
        raise MapUnavailable("原稿の場所を国境データで描けませんでした。図解など別の種類を選べます。")
    return notes
