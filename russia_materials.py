"""ロシアの素材選びにある、図解の手直しと種類を選んだ候補追加。"""
import json

from PIL import Image


BACKGROUND = "#D9E1E8"
NEAR_BLACK = 38
BACKGROUND_RULE = (
    "Russia channel background requirement: use a light, opaque, full-frame background. "
    f"For graphics use {BACKGROUND} as the base and #A8B9C4 as the secondary background. "
    "Use #1B365D for main elements, #2C4C3B for comparisons, #E5A91A for neutral, "
    "#A6192E for warnings and #B7950B for emphasis. The color hierarchy may vary. Keep saturation low. "
    "Keep real scenes naturally lit. Never use a black, near-black, dark navy or transparent backdrop, "
    "including behind the cards and in the margins. Replace such a backdrop even in a source image "
    "being edited. Retain dark details and legible labels. Never print color codes in the image."
)
BLACK_BACKGROUND_MESSAGE = "生成画像の背景が黒いため、候補に追加しませんでした。"


class BlackBackground(ValueError):
    pass


def has_black_backdrop(image):
    """A black surround occupies both the frame edges and a substantial area, unlike dark labels."""
    with image.resize((48, 27), Image.Resampling.BOX) as sample:
        pixels = sample.load()
        dark = edge_dark = 0
        w, h = sample.size
        for y in range(h):
            for x in range(w):
                if max(pixels[x, y]) <= NEAR_BLACK:
                    dark += 1
                    if x in (0, w - 1) or y in (0, h - 1):
                        edge_dark += 1
        return edge_dark >= .60 * (2 * w + 2 * h - 4) and dark >= .30 * w * h


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
