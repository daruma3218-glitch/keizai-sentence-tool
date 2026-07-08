#!/usr/bin/env python3
"""Phase 2: センテンス → 英文画像プロンプト（並列バッチ）

各センテンスをシンプルな「フラットインフォグラフィック」に変換する。
原稿の数値・年代・固有名詞は積極的にホワイトリスト化して画像内テキストとして使う。
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from typing import Callable, Optional

import anthropic

from utils import cached_user_content, claude_query, parse_json_array


CLAUDE_MODEL = "claude-sonnet-5"
BATCH_SIZE = 8
PROMPTER_BATCH_TIMEOUT_SECONDS = 90
PROMPTER_OVERALL_TIMEOUT_SECONDS = 360


DIAGRAM_CONNECTOR_TERMS = [
    "原因", "結果", "背景", "変化", "影響", "依存", "支援", "圧力",
    "対立", "比較", "流れ", "転換", "選択肢", "支配", "統治", "供給",
    "需要", "制裁", "回避", "現在", "過去",
]


# ===== 安全な自動抽出（Claude の遠慮を補完） =====
# 原稿に出てくる以下のパターンは無条件で allowed_terms に追加してよい:
#   年代 (1858年, 2024年, 19世紀)
#   数値 (100年, 14か国, 1.5億, 65万8千人, 4,380km, 6.3%, 75%, 1兆530億ドル)
#   括弧内の固有名詞 (「大陸帝国」「ヴィア」)
SAFE_PATTERNS = [
    # 年代
    r'\d{1,4}年(?:代)?',                  # 1858年, 1858年代
    r'\d{1,2}世紀',                       # 19世紀
    # 数値+単位
    r'\d+(?:,\d{3})*(?:\.\d+)?(?:%|％)',  # 6.3%, 75%
    r'\d+(?:,\d{3})*(?:\.\d+)?(?:億|兆|万|千)?(?:円|ドル|人|km|キロメートル|平方キロメートル|か国)',
    r'\d+(?:,\d{3})*年(?:以上|間|前)',    # 100年以上
    # 括弧内のキーワード（「」『』内の短い語）
    r'「([^」]{2,15})」',
    r'『([^』]{2,15})』',
    # カタカナ固有名詞（ロシア、ベラルーシ、ウクライナ等）
    r'[ァ-ヴー]{3,15}',
    # 政治・経済・地政学で図解ラベルになりやすい複合語
    r'[一-龥]{2,8}(?:依存|支援|圧力|制裁|侵攻|統治|供給|需要|対立|関係|崩壊|独立|同盟|戦争)',
]


DIAGRAM_DESIGN_RULES_JA = """【図解品質ルール（diagram / 図解として描く illustration では最重要）】
- まず「この図で何を理解させるか」を1つに絞る。雰囲気や単語の羅列ではなく、主張が伝わる図にする
- 読む順番を必ず決める（左→右、上→下、中心→周辺のいずれか）。視線誘導がない散らばった構図は禁止
- 3〜5個の要素を、因果・比較・時系列・対立・依存関係のどれか1つの構造で接続する
- 画像内ラベルは allowed_terms からだけ選ぶ。原因/結果/背景/変化/影響/依存/対立/比較/流れなどの抽象的な構造語を、原稿にないのに追加表示してはいけない
- 関係性や役割は、構造語ラベルではなく、矢印・線・囲み・位置関係・大小差で表現する
- 各ラベルが「役割」を持つように置く。ラベルだけを追っても内容の流れが分かる状態にする
- 矢印・線・囲み・位置関係で、なぜその要素同士がつながるのかを表現する
- 孤立したキーワードカード、意味のないアイコン集合、長文説明、重複文字、見出しだけの図は禁止"""


DIAGRAM_PROMPT_REQUIREMENTS_EN = (
    "For any diagram-like image, the prompt MUST specify: the single visual goal, "
    "one clear reading path (left-to-right, top-to-bottom, or center-out), 3 to 5 "
    "connected elements, the relationship between those elements, and where the "
    "allowed labels go. The viewer should understand the argument by following "
    "the labels and arrows, not by guessing from isolated keywords."
)


DIAGRAM_STRUCTURE_TEMPLATES = {
    "cause_effect": "Template: left cause -> center mechanism/change -> right result, with one arrow chain.",
    "comparison": "Template: two balanced columns with the same 2-3 criteria, plus a small conclusion label.",
    "timeline": "Template: three chronological milestones on one horizontal line, oldest to newest.",
    "opposition": "Template: left actor vs right actor, central issue/tension, opposing arrows.",
    "dependency": "Template: dependent side -> dependency channel/resource -> controlling/supporting side.",
    "process": "Template: step 1 -> step 2 -> step 3, one clear process flow.",
    "relationship": "Template: 3-5 nodes connected by labeled arrows showing one relationship network.",
}


ROSHIA_KAIZEN_BLOCK_JA = """【ロシア解体新書: カイゼン品質ゲート】
日本的な制作カイゼンとして、diagram は次の標準作業で設計する:
- 整理: 1枚1主張。装飾、曖昧な背景、意味の薄いアイコンを削る
- 整頓: 左→右または中心→周辺に並べ、ラベルと矢印だけで内容が追える配置にする
- 標準化: 地政学・歴史・政策は下の型から最も近いものを選ぶ
- ポカヨケ: 日本地図、日本列島、円マーク、東京、日本風の街並み、可愛い表情、動物寓意を出さない
- アンドン: うまく型に入らない時は、無理に複雑化せず「3ノードの関係図」に落とす

ロシア解体新書で優先する図解型:
- 従属関係図: 周辺国 → 軍事/エネルギー/通貨/情報 → ロシア
- 緩衝地帯図: ロシア中心 → 周辺圏 → NATO/欧米側圧力
- 帝国拡張/縮小図: 過去 → 転換点 → 現在
- 資源依存図: 供給源 → パイプライン/輸送路 → 依存先
- 制裁回避/迂回図: 制裁 → 迂回ルート → 影響
- 国内統治構造図: 権力中枢 → 治安/メディア/制度 → 社会
- 選択肢消失図: 選択肢A/B/Cが狭まり、最後に依存または衝突へ収束
"""


def _is_roshia_instruction(user_instructions: str) -> bool:
    text = user_instructions or ""
    return "ロシア解体新書" in text or "ロシア・旧ソ連" in text or "Russia, the USSR" in text


def _infer_diagram_structure(sentence: str) -> str:
    """文面から図解の既定構造を少し賢く選ぶ。"""
    s = sentence or ""
    if any(w in s for w in ("依存", "支援", "従属", "影響下", "結びつけ", "パイプライン", "通貨", "供給")):
        return "dependency"
    if any(w in s for w in ("対立", "戦争", "侵攻", "圧力", "制裁", "NATO", "欧米", "反発")):
        return "opposition"
    if len(re.findall(r'\d{3,4}年', s)) >= 1 or any(w in s for w in ("以降", "その後", "崩壊", "成立", "転換")):
        return "timeline"
    if any(w in s for w in ("ため", "ので", "結果", "背景", "原因", "なぜ")):
        return "cause_effect"
    if any(w in s for w in ("比較", "一方", "対して", "より", "違い")):
        return "comparison"
    if any(w in s for w in ("手順", "流れ", "プロセス", "段階")):
        return "process"
    return "relationship"


def _auto_extract_terms(sentence: str) -> list:
    """センテンスから安全な語句を自動抽出（Claude の補完用）"""
    terms = []
    seen = set()
    for pat in SAFE_PATTERNS:
        for m in re.finditer(pat, sentence):
            # キャプチャグループがあればそれを使う
            t = m.group(1) if m.groups() else m.group(0)
            t = t.strip()
            if t and t not in seen and t in sentence:
                seen.add(t)
                terms.append(t)
    return terms


def _limit_allowed_terms(terms: list, sentence: str, max_terms: int = 6, allow_connectors: bool = False) -> list:
    """画像内テキストを増やしすぎないため、重要語だけに絞る。"""
    scored = []
    seen = set()
    connector_set = set(DIAGRAM_CONNECTOR_TERMS)
    for t in terms:
        if not isinstance(t, str):
            continue
        t = t.strip()
        is_connector = allow_connectors and t in connector_set
        if not t or t in seen or (not is_connector and t not in sentence):
            continue
        seen.add(t)
        if is_connector and t not in sentence:
            scored.append((1, len(scored), t))
            continue
        score = 0
        if is_connector:
            score += 3
        if re.search(r'\d', t):
            score += 5
        if len(t) <= 10:
            score += 2
        if any(k in t for k in ("年", "％", "%", "ドル", "円", "人", "国", "ロシア", "ソ連")):
            score += 2
        score += max(0, 12 - len(t))
        scored.append((score, len(scored), t))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [t for _, _, t in scored[:max_terms]]


def _fallback_diagram_blueprint(row: dict, allowed_terms: Optional[list] = None) -> dict:
    """diagram 用の最低限の設計図。Claude失敗時でもキーワード羅列を避ける。"""
    sent = row.get("sentence", "")
    terms = _limit_allowed_terms(
        (allowed_terms or []) + _auto_extract_terms(sent),
        sent,
        allow_connectors=False,
    )
    elements = terms[:3] if terms else ["主要要素", "関係先", "変化先"]
    labels = terms[:6]
    structure = _infer_diagram_structure(sent)
    bp = {
        "visual_goal": f"この文の要点を因果または関係性として理解させる: {sent[:80]}",
        "structure": structure,
        "reading_path": "left-to-right",
        "elements": elements,
        "relationships": ["左から右へ、要素同士の関係を矢印と配置で接続する。抽象的な構造語ラベルは置かない"],
        "labels": labels,
        "forbidden": ["キーワード羅列", "長文説明", "重複文字", "孤立したカード", "詳細地図"],
    }
    bp["template"] = DIAGRAM_STRUCTURE_TEMPLATES.get(bp["structure"], DIAGRAM_STRUCTURE_TEMPLATES["relationship"])
    return bp


def _normalize_diagram_blueprint(value, row: dict, allowed_terms: Optional[list] = None) -> dict:
    """Claudeが返した diagram_blueprint を画像生成で使える小さなJSONに整える。"""
    if not isinstance(value, dict):
        return _fallback_diagram_blueprint(row, allowed_terms)

    sent = row.get("sentence", "")
    terms = _limit_allowed_terms(
        list(value.get("labels") or []) + list(allowed_terms or []) + _auto_extract_terms(sent),
        sent,
        allow_connectors=False,
    )

    def clean_str(v, default=""):
        return str(v).strip()[:140] if v is not None and str(v).strip() else default

    def clean_list(v, limit=5):
        if not isinstance(v, list):
            return []
        out = []
        seen = set()
        for item in v:
            s = clean_str(item)
            if s and s not in seen:
                seen.add(s)
                out.append(s)
            if len(out) >= limit:
                break
        return out

    structure = clean_str(value.get("structure"), _infer_diagram_structure(sent))
    if structure not in ("cause_effect", "comparison", "timeline", "opposition", "dependency", "process", "relationship"):
        structure = "relationship"
    reading_path = clean_str(value.get("reading_path"), "left-to-right")
    if reading_path not in ("left-to-right", "top-to-bottom", "center-out"):
        reading_path = "left-to-right"

    bp = {
        "visual_goal": clean_str(value.get("visual_goal")) or _fallback_diagram_blueprint(row, terms)["visual_goal"],
        "structure": structure,
        "reading_path": reading_path,
        "elements": clean_list(value.get("elements"), 5) or (terms[:3] if terms else ["主要要素", "関係先", "変化先"]),
        "relationships": clean_list(value.get("relationships"), 4) or ["要素同士の関係を矢印で接続する"],
        "labels": terms[:6],
        "forbidden": clean_list(value.get("forbidden"), 6) or ["キーワード羅列", "長文説明", "重複文字"],
    }
    bp["template"] = DIAGRAM_STRUCTURE_TEMPLATES.get(structure, DIAGRAM_STRUCTURE_TEMPLATES["relationship"])
    if _diagram_blueprint_is_weak(bp):
        fallback = _fallback_diagram_blueprint(row, terms)
        fallback["structure"] = structure
        fallback["template"] = DIAGRAM_STRUCTURE_TEMPLATES.get(structure, DIAGRAM_STRUCTURE_TEMPLATES["relationship"])
        return fallback
    return bp


def _diagram_blueprint_is_weak(blueprint: dict) -> bool:
    """画像生成前に弱い設計を弾く。APIを増やさず、弱いものはフォールバック設計へ寄せる。"""
    if not isinstance(blueprint, dict):
        return True
    goal = str(blueprint.get("visual_goal", "")).strip()
    elements = blueprint.get("elements") or []
    relationships = blueprint.get("relationships") or []
    if len(goal) < 12:
        return True
    if len(elements) < 3:
        return True
    if not relationships:
        return True
    generic = {"要点", "背景", "結果"}
    if set(elements).issubset(generic) and not blueprint.get("labels"):
        return True
    return False


def _blueprint_prompt_fragment(blueprint: dict) -> str:
    """設計JSONを画像生成プロンプトへ短く埋め込む。"""
    if not isinstance(blueprint, dict):
        return ""
    labels = ", ".join(blueprint.get("labels") or [])
    elements = "; ".join(blueprint.get("elements") or [])
    rels = "; ".join(blueprint.get("relationships") or [])
    forbidden = "; ".join(blueprint.get("forbidden") or [])
    return (
        " Diagram blueprint to follow exactly: "
        f"visual goal = {blueprint.get('visual_goal', '')}; "
        f"structure = {blueprint.get('structure', '')}; "
        f"template = {blueprint.get('template', '')}; "
        f"reading path = {blueprint.get('reading_path', '')}; "
        f"elements = {elements}; relationships = {rels}; "
        f"fact labels should use these Japanese labels = {labels}; "
        "do not display generic structural labels such as 原因, 結果, 背景, 影響, 依存, 対立, 比較, 流れ unless that exact word appears in the source sentence and is listed as a fact label; express structure with arrows, grouping, and placement instead; "
        f"avoid = {forbidden}."
    )


def _build_user_block(user_instructions: str, style_preset: str) -> str:
    blocks = []
    if user_instructions.strip():
        blocks.append(f"""【ユーザーからの画像指示（最優先で従うこと）】
{user_instructions.strip()}""")

    style_descriptions = {
        "flat_infographic": (
            "【スタイル: フラットインフォグラフィック（最優先で守ること）】\n"
            "- YouTubeの教養チャンネルにふさわしい、落ち着いた知的な資料感にする\n"
            "- 配色は背景色 #D9E1E8、サブ背景色 #A8B9C4、メイン #1B365D、サブ/比較 #2C4C3B、中立色 #E5A91A、警告色 #A6192E、注目色 #B7950B をベースにする\n"
            "- 色の階層は変えてよいが、高彩度色は禁止。カラーコードを画像内に表示しない\n"
            "- 可読性を高めるため、必要に応じてテキストの後ろに角丸シェイプを配置する\n"
            "- アイコン・記号ベース（人型シルエット、国旗、矢印、囲み、比較パネルなど）\n"
            "- キーワードの羅列は禁止。必ず因果・比較・時系列・関係性のどれか1つの構造にする。ただし『原因』『結果』『流れ』などの抽象ラベルを画像内に表示しない\n"
            "- 図解は読む順番を明確にし、ラベルと矢印を追えば内容が入ってくる構成にする\n"
            "- 図解の画像内テキストは最大6語まで。同じ語を重複表示しない\n"
            "- 情報を整理し、誤字・脱字・重複文字を出さない。インフォグラフィックは見やすくシンプルにする\n"
            "- 写実画・寓意（動物の擬人化）・劇的演出は避ける\n"
            "- ニュース番組のテロップ・統計レポートのような「説明画面」を目指す"
        ),
        "pictogram": (
            "【スタイル: ピクトグラム調】\n"
            "- 単色シルエット（黒または1色）の人型・物のアイコンのみ\n"
            "- 余計な装飾なし、最大シンプル\n"
            "- 公共表示・トイレマーク的な明快さ"
        ),
        "comic": (
            "【スタイル: コミックストリップ調】\n"
            "- マンガ風セル割り（ただし吹き出しのテキストは allowed_terms 内のみ）\n"
            "- 4色程度のフラットカラー\n"
            "- 線がはっきり太く、表情の分かるキャラクター"
        ),
        "whiteboard": (
            "【スタイル: 手描きホワイトボード調】\n"
            "- 白背景に黒マジック手描き風\n"
            "- ラフな矢印・囲み・吹き出し\n"
            "- TED チャンネル・Sketchnoting のような図解"
        ),
        "soviet_propaganda": (
            "【スタイル: ソ連プロパガンダ風（歴史的スタイル再現）】\n"
            "- 1920-1950年代の構成主義 + 社会主義リアリズム風の教育ポスター\n"
            "- 深い赤・純黒・肌色オフホワイトの3色を中心に、フラット塗り・グラデなし\n"
            "- 低視点、対角線構図、英雄的シルエット、リトグラフ印刷の紙質感\n"
            "- 武器・ハンマー&鎌・赤い星・暴力表現は使わない\n"
            "- 書物・地球儀・分析装置・建築など、教育/分析のシンボルで表現する"
        ),
    }
    blocks.append(style_descriptions.get(style_preset, style_descriptions["flat_infographic"]))
    return "\n\n".join(blocks)


def _fallback_prompt_for_row(row: dict) -> dict:
    """Claudeプロンプト生成が失敗した行を止めずに進めるための機械プロンプト。"""
    sent = row.get("sentence", "")
    row_type = row.get("route") or row.get("type") or "illustration"
    if row_type not in ("illustration", "realphoto", "map", "diagram", "chart", "decorative"):
        row_type = "illustration"
    auto_terms = _limit_allowed_terms(
        _auto_extract_terms(sent),
        sent,
        allow_connectors=False,
    )
    text_rule = (
        f"Allowed Japanese text only: {', '.join(auto_terms)}. Do not add any other text."
        if auto_terms else
        "No text in image, no labels, no numbers."
    )
    type_hint = {
        "realphoto": "Photorealistic documentary photograph, natural lighting, realistic textures.",
        "map": "Clear 16:9 map or terrain visualization, readable borders and route lines.",
        "diagram": (
            "Clear flat educational diagram with one visual goal, a left-to-right reading path, "
            "3 connected labeled nodes, and arrows that explain the cause-effect or comparison."
        ),
        "chart": "Simple clean chart based only on numbers from the sentence.",
        "illustration": "Simple flat educational illustration.",
        "decorative": "Minimal neutral educational background.",
    }.get(row_type, "Simple flat educational illustration.")
    blueprint = _normalize_diagram_blueprint(
        row.get("diagram_blueprint"),
        row,
        auto_terms,
    ) if row_type == "diagram" else None
    return {
        "no": row["no"],
        "prompt": (
            f"{type_hint} Explain this Japanese sentence visually: {sent[:160]}. "
            f"{_blueprint_prompt_fragment(blueprint) if blueprint else ''} "
            f"{DIAGRAM_PROMPT_REQUIREMENTS_EN if row_type == 'diagram' else ''} "
            f"{text_rule} 16:9 landscape composition, clean layout, no invented facts."
        ),
        "type": row_type,
        "allowed_terms": auto_terms,
        "diagram_blueprint": blueprint or {},
        "character": False,
    }


def generate_prompts_batch(
    client: anthropic.Anthropic,
    rows_batch: list,
    title: str,
    user_instructions: str = "",
    style_preset: str = "flat_infographic",
    worldview_desc: str = "",
) -> list:
    """1 バッチのセンテンスを英文プロンプト化"""
    user_block = _build_user_block(user_instructions, style_preset)
    kaizen_block = f"\n\n{ROSHIA_KAIZEN_BLOCK_JA}" if _is_roshia_instruction(user_instructions) else ""
    # 世界観・キャラ統一の指示（illustration/diagram/decorative に適用）
    worldview_block = ""
    if worldview_desc.strip():
        worldview_block = f"""

【世界観・キャラクター統一（最重要・illustration / diagram / decorative にのみ適用）】
人物や情景を描くイラストでは、以下の世界観・キャラクター設定を**毎回一貫して**反映すること。
登場人物・画風・色調・タッチを動画全体で統一し、シーンが変わっても同じ世界観に見せる:
---
{worldview_desc.strip()}
---
※ realphoto（実写）・chart（グラフ）にはこの世界観を適用しない（実写・数値はそのまま）。
※ 人物が登場する illustration では必ず上のキャラクター設定の人物を使う。"""

    # Claude に渡す入力 + 自動抽出済み terms をヒントとして同梱
    # 各行の route（ルーター判定）を type として固定で渡す
    inputs = []
    for r in rows_batch:
        sent = r.get("sentence", "")
        hints = _auto_extract_terms(sent)
        row_type = r.get("route") or r.get("type") or "illustration"
        inputs.append({
            "no": r["no"],
            "type": row_type,  # ★この type を厳守すること（変更禁止）
            "chapter": r.get("chapter_title", ""),
            "block_context": r.get("block_text", "")[:400],
            "near_context": r.get("diagram_context", "")[:700],
            "sentence": sent,
            "auto_extracted_terms": hints,  # ヒント
            "visual_hint": r.get("visual_hint", ""),
        })
    inputs_json = json.dumps(inputs, ensure_ascii=False, indent=2)

    system = (
        "You are a visual director. You convert each Japanese sentence from a video "
        "manuscript into a precise English image prompt. Each item has a fixed 'type' "
        "you MUST honor: realphoto = a realistic documentary PHOTOGRAPH, map = a realistic "
        "satellite/terrain MAP, and illustration/diagram/chart = the specified graphic style. "
        "Return only a JSON array. No markdown, no commentary."
    )

    # 固定ルール部（全バッチ・全ジョブ共通）を先頭に置き prompt cache 対象にする。
    # タイトル・入力・チャンネル別の指示/世界観など動的な内容は後段へ分離。
    fixed_prompt_rules = (
        """各センテンス（1文）に対応する英文画像プロンプトを書いてください。
**各項目の type は厳守**（変更禁止）。type ごとに描き方が違います。

"""
        + DIAGRAM_DESIGN_RULES_JA
        + """

【diagram_blueprint（図解設計JSON）】
type が diagram の項目は、画像プロンプトを書く前に必ず diagram_blueprint を作ること。
これは図解ツクールのように「何を理解させる図か」を先に固定するための設計図です。
diagram_blueprint は以下の形式にする:
{
  "visual_goal": "この図で理解させる1つの主張",
  "structure": "cause_effect | comparison | timeline | opposition | dependency | process | relationship",
  "reading_path": "left-to-right | top-to-bottom | center-out",
  "elements": ["3〜5個の構成要素"],
  "relationships": ["要素同士をどう結ぶか。矢印・対比・依存など"],
  "labels": ["画像内に置く短い日本語ラベル。allowed_terms の語だけ。抽象的な構造語を追加しない"],
  "forbidden": ["避ける表現。例: キーワード羅列, 長文説明, 重複文字"]
}
diagram の prompt は必ずこの blueprint に沿って書くこと。
prompt 内にも visual goal / reading path / 3-5 connected elements / relationship / label placement を具体的に含めること。
near_context は visual_goal・structure・relationships の理解に使ってよいが、新しい事実・固有名詞・数字は追加しないこと。
diagram の画像内ラベルは、対象 sentence 由来の allowed_terms だけを使うこと。原因/結果/背景/変化/影響/依存/支援/圧力/対立/比較/流れ/転換などの抽象的な構造語を、原稿にないのに追加表示してはいけない。関係性は矢印・配置・囲み・線の向きで表す。
type が diagram 以外の項目では diagram_blueprint は空オブジェクト {} にする。

【structure別テンプレート】
- cause_effect: 原因 → 仕組み/変化 → 結果
- comparison: 左右比較 + 同じ評価軸 + 小さな結論
- timeline: 3点だけの横時系列
- opposition: 左右対立 + 中央の争点
- dependency: 依存する側 → 依存経路/資源 → 支える/支配する側
- process: 手順1 → 手順2 → 手順3
- relationship: 3〜5ノードの関係図

【最重要: type 別の描き方】
- **realphoto**: 実写写真。"photorealistic documentary photograph, real photo, natural lighting,
  realistic textures, cinematic" を必ず含める。**フラット/アイコン/イラストには絶対しない**。
  上で指定したグラフィックスタイル（フラット等）は realphoto には適用しないこと。
  **日本語ラベルは入れない**。画面内の看板・標識は描かれている場所の現地語
  （ロシア/ソ連のシーンならロシア語＝キリル文字）にすること。
  プロンプトに "signs and text in the local language (Russian/Cyrillic for Russia), no Japanese labels" と明記。
- **map**: リアルな衛星・地形図。"realistic satellite map, terrain, natural earth colors" を含める。
  フラットな地図にはしない。上で指定したグラフィックスタイルは map には適用しないこと。
- **illustration / diagram / chart / decorative**: 上で指定したグラフィックスタイルに従って描く。
- visual_hint がある項目は必ず従う。特に「no map outlines」とある場合は、地図・国境線の細密描写ではなく、
  矢印・領域ブロック・回廊・短いラベルで位置関係を説明するシンプルな図解にする。

【最重要ルール】
1. プロンプトは英語で書く
2. メタファー（クマ＝ロシア など寓意）は**禁止**。国は国旗・国名・地図で直接表現する
3. **画像内テキスト**: 固有名詞・国名・人物名・数値などの事実ラベルは allowed_terms のみ。diagram でも原因/結果/背景/影響/流れなどの抽象的な構造語を追加表示しない
4. 出力の type は入力の type を**そのまま返す**（勝手に変えない）
5. **character フラグ**: 世界観設定に「繰り返し登場する固定キャラ（先生／教授／解説役）」
   がある場合、そのキャラが実際に画面に描かれる illustration のときだけ "character": true。
   図表(diagram/chart)・写真(realphoto)・地図(map)・人物のいないシーン・装飾は必ず false。

【allowed_terms 抽出方針（積極的に入れる）】
- センテンスに登場する以下は**すべて** allowed_terms に入れること:
  * 年代 (1858年, 19世紀, 1945年)
  * 数値 (100年以上, 14か国, 65万8千人, 6.3%, 1兆530億ドル)
  * 国名 (ロシア, 中国, アメリカ, 日本, ソ連, モンゴル)
  * 地名 (ウラジオストク, 北京, アイグン, 満州)
  * 人名 (スターリン, 毛沢東, ニクソン)
  * 重要なキーワード (大陸帝国, 二正面作戦, アヘン戦争, 北京条約)
- **auto_extracted_terms** はすでに抽出済みなので必ず取り込み、加えてセンテンスからも追加抽出する
- 一般語（「国」「時」「これ」など）は除外
- ただし allowed_terms は最大6語まで。多すぎる場合は、数字・国名・人名・地名を優先する
- 同じ語・同じ数字を画像内に複数回表示してはいけない

【画像内テキストの記述例】
- allowed_terms = ["ロシア", "中国", "100年"] の場合:
  "Insert these Japanese labels prominently in the image: ロシア, 中国, 100年. Do NOT add any other Japanese or English text."
- allowed_terms = [] の場合:
  "No text in image, no labels, no numbers. Use icons only."

【type の選び方】
- illustration: 人物・物・出来事のイラスト（**シンプルアイコン調**）
- realphoto: **実写風の写真**。都市・建物・施設・インフラ・事件・戦争・人々の生活など
  物理的なシーンを、ドキュメンタリー写真のようにリアルに描く（イラストではなく本物の写真）。
  自然光・実在感のある質感・映画的構図。報道写真／ドキュメンタリー品質を目指す。
- map: 地理関係。**フラットな図ではなく、衛星写真／航空写真のようなリアルな地図**にすること。
  上空から見た本物の地球表面（青い海・緑の森林・茶色の山岳・白い雪原・リアルな海岸線）を描き、
  地形の起伏（レリーフシェーディング）も表現する。対象の国・地域は半透明の色で塗り分け、
  国境は細い線で示す。Google Earth / NASA衛星画像 / ナショナルジオグラフィック品質を目指す。
- diagram: 概念図・フロー図（アイコン + 矢印 + ラベル）。キーワード羅列ではなく、因果・比較・流れ・関係性で見せる。
  地理・領土・ルート・勢力圏を扱う場合も、詳細地図ではなく、矢印・領域ブロック・回廊・短いラベルの位置関係図解にする。
  prompt 内に必ず「visual goal」「reading path」「3-5 connected elements」「relationship between elements」「allowed label placement」を英語で具体的に書く
- chart: 数値比較（棒グラフ・円グラフ・大きな数字）。チャンネル指示でグラフ禁止の場合は diagram として扱う
- decorative: 接続詞・挨拶・抽象表現（背景パターン）

【出力 JSON】
[
  {
    "no": (元のno),
    "prompt": "英語プロンプト（スタイル指示・テキスト制約を必ず含む）",
    "type": "illustration | realphoto | map | diagram | chart | decorative",
    "allowed_terms": ["積極的に抽出した語"],
    "diagram_blueprint": {} または上記形式の設計JSON,
    "character": true または false（ルール5。固定キャラ＝先生/教授/解説役が描かれる illustration のみ true）
  },
  ...
]
JSON のみ。"""
    )

    dynamic_prompt_payload = f"""動画原稿「{title}」

入力（type=その項目の描画種別。near_context は設計判断用の前後文脈。画像内ラベルは sentence / allowed_terms 由来に限定）:
{inputs_json}

{user_block}{kaizen_block}{worldview_block}

必ず {len(rows_batch)} 件返すこと（順序は入力と同じ）。"""

    query = cached_user_content(
        (fixed_prompt_rules, True),
        (dynamic_prompt_payload, False),
    )

    result = claude_query(
        client,
        query,
        system,
        max_tokens=8000,
        model=CLAUDE_MODEL,
        max_retries=1,
        timeout_seconds=PROMPTER_BATCH_TIMEOUT_SECONDS,
    )
    prompts = parse_json_array(result)

    # 入力情報をマージ + allowed_terms をセンテンス検証
    prompts_by_no = {p.get("no"): p for p in prompts if p.get("prompt")}
    merged = []
    for r in rows_batch:
        no = r["no"]
        sent = r.get("sentence", "")
        auto_terms = _auto_extract_terms(sent)
        if no in prompts_by_no:
            p = prompts_by_no[no]
            # allowed_terms 検証 + auto_terms を追加
            terms = p.get("allowed_terms", [])
            if not isinstance(terms, list):
                terms = []
            # 既存 + auto をマージ
            merged_terms = list(terms) + auto_terms
            verified = []
            seen = set()
            for t in merged_terms:
                if not isinstance(t, str):
                    continue
                t = t.strip()
                if t and t in sent and t not in seen:
                    seen.add(t)
                    verified.append(t)
            # type はルーターの route を最優先で固定（Claudeが勝手に変えても上書き）
            forced_type = r.get("route") or r.get("type")
            if forced_type in ("illustration", "realphoto", "map", "diagram", "chart", "decorative"):
                p["type"] = forced_type
            elif p.get("type") not in ("illustration", "realphoto", "map", "diagram", "chart", "decorative"):
                p["type"] = "illustration"
            p["allowed_terms"] = _limit_allowed_terms(
                verified,
                sent,
                allow_connectors=False,
            )
            # character フラグは illustration のときだけ有効（図表/写真/地図/装飾では必ず False）
            p["character"] = bool(p.get("character", False)) and p["type"] == "illustration"
            if p["type"] == "diagram":
                bp = _normalize_diagram_blueprint(p.get("diagram_blueprint"), r, p["allowed_terms"])
                p["diagram_blueprint"] = bp
                fragment = _blueprint_prompt_fragment(bp)
                if fragment and "Diagram blueprint to follow exactly" not in p.get("prompt", ""):
                    p["prompt"] = f"{p.get('prompt', '')} {fragment}"
            else:
                p["diagram_blueprint"] = {}
            merged.append(p)
        else:
            # フォールバック
            short = sent[:80]
            row_type = r.get("route") or r.get("type") or "decorative"
            bp = _fallback_diagram_blueprint(r, auto_terms) if row_type == "diagram" else {}
            fallback_terms = _limit_allowed_terms(
                auto_terms,
                sent,
                allow_connectors=False,
            )
            fallback_text_rule = (
                f"Use only these Japanese labels: {', '.join(fallback_terms)}. Do NOT add generic structural labels such as 原因, 結果, 背景, 影響, 依存, 対立, 比較, 流れ unless they are explicitly listed here."
                if fallback_terms else
                "No text in image, no labels, no numbers."
            )
            fallback_prompt = (
                f"Flat infographic explaining: {short}. "
                "Simple icons, 2-3 flat colors (navy blue, white, light gray). "
                "Bold layout. No metaphors. "
                f"{_blueprint_prompt_fragment(bp) if bp else ''} "
                f"{fallback_text_rule} "
                "16:9 landscape orientation, no title text."
            )
            merged.append({
                "no": no,
                "prompt": fallback_prompt,
                "type": row_type if row_type in ("illustration", "realphoto", "map", "diagram", "chart", "decorative") else "decorative",
                "allowed_terms": fallback_terms,
                "diagram_blueprint": bp,
                "character": False,
            })
    return merged


def generate_all_prompts(
    client: anthropic.Anthropic,
    rows: list,
    title: str,
    user_instructions: str = "",
    style_preset: str = "flat_infographic",
    worldview_desc: str = "",
    max_workers: int = 6,
    log: Optional[Callable] = None,
) -> list:
    """全センテンスを並列バッチで英文プロンプト化"""
    log = log or (lambda *a, **kw: None)

    batches = []
    for i in range(0, len(rows), BATCH_SIZE):
        batches.append(rows[i:i + BATCH_SIZE])

    diagram_count = sum(1 for r in rows if (r.get("route") or r.get("type")) == "diagram")
    log("prompter", f"{len(rows)} センテンスを {len(batches)} バッチに分割（同時 {max_workers} 並列）/ style={style_preset}"
                    + (f"／図解設計JSON {diagram_count} 件" if diagram_count else "")
                    + ("／世界観統一ON" if worldview_desc.strip() else ""))

    prompts_by_no = {}
    completed = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_idx = {
            executor.submit(generate_prompts_batch, client, batch, title, user_instructions, style_preset, worldview_desc): i
            for i, batch in enumerate(batches)
        }
        batches_by_idx = {i: batch for i, batch in enumerate(batches)}
        def fallback_batch(idx: int, reason: str):
            nonlocal completed
            fallback = [_fallback_prompt_for_row(r) for r in batches_by_idx.get(idx, [])]
            for p in fallback:
                prompts_by_no[p["no"]] = p
            completed += 1
            log("warn", f"プロンプトバッチ {idx + 1}/{len(batches)} が失敗/タイムアウト。機械プロンプトで続行します: {reason[:120]}")
            log("prompter", f"バッチ {completed}/{len(batches)} 完了（フォールバック {len(fallback)} 件）")

        try:
            iterator = as_completed(future_to_idx, timeout=max(PROMPTER_OVERALL_TIMEOUT_SECONDS, len(batches) * 25))
            for future in iterator:
                idx = future_to_idx[future]
                try:
                    results = future.result(timeout=1)
                    for p in results:
                        if p.get("no") is not None:
                            prompts_by_no[p["no"]] = p
                    completed += 1
                    log("prompter", f"バッチ {completed}/{len(batches)} 完了（{len(results)} 件）")
                except Exception as e:
                    fallback_batch(idx, str(e))
        except FuturesTimeout:
            log("warn", "プロンプト生成が全体時間上限に達しました。未完了バッチは機械プロンプトで続行します")
        finally:
            for future, idx in future_to_idx.items():
                if future.done():
                    continue
                future.cancel()
                fallback_batch(idx, "全体タイムアウト")

    out_rows = []
    for r in rows:
        no = r["no"]
        p = prompts_by_no.get(no, {})
        merged_row = dict(r)
        if not p.get("prompt"):
            p = _fallback_prompt_for_row(r)
        merged_row["prompt"] = p.get("prompt", "")
        merged_row["type"] = p.get("type", "illustration")
        merged_row["allowed_terms"] = p.get("allowed_terms", [])
        merged_row["diagram_blueprint"] = p.get("diagram_blueprint", {})
        merged_row["character"] = bool(p.get("character", False))  # キャラ固定フラグを引き継ぐ
        out_rows.append(merged_row)

    return out_rows
