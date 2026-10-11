"""Content-led choices among fixed, reviewed presenter sprites; never random."""
import re

POSE_NAMES = {"pointing": "指差し", "thinking": "考える", "surprised": "驚く",
              "explaining": "両手で説明"}
POSE_PLANNING_INSTRUCTION = """
【すあし社長の表情・ポーズ（図の内容から選ぶ）】
各 illustration / diagram の出力JSONに suasi_pose と suasi_pose_reason を追加する。
suasi_pose は pointing / thinking / surprised / explaining のどれか。
pointing=仕組み・関係・比較の通常説明（迷ったらこれ）。
thinking=未解決の問い、判断条件、選択肢を考える場面。文中の「なぜ」に答えている説明ならpointing。
surprised=原稿が明確に示す意外な事実・予想との逆転。単に大きな数値や損失というだけで驚かせない。
explaining=全体像、手順の整理、要点のまとめ・結論。
事故・災害・戦争の被害、死傷、自殺、病気の苦痛などにはsurprisedを使わずpointing。
suasi_pose_reasonには対象文のどの意味に合わせたかを日本語1文で書く。単調回避のためにランダムで変えない。
画像AIは図だけを描き、選んだ固定素材は別工程で配置する。キャラの表情やポーズを描く指示を画像プロンプトへ入れない。
"""

# Conservative guard for tone: even a planner suggestion must not turn distress
# into a cheerful reaction. This guard reads source content, not style boilerplate.
SENSITIVE = re.compile(r"死[亡者傷]|命を失|命を落|犠牲|自殺|虐待|被災|大震災|津波|大洪水|空襲|虐殺|病気|がん患者|癌患者|死亡事故|戦争.{0,12}(?:被害|被災|苦し)|(?:fatal|killed|suicide|casualt|massacre|victim|terminal illness)", re.I)


def select_pose(entry):
    sentence = str(entry.get("sentence") or entry.get("excerpt") or entry.get("keypoint") or "")
    context = sentence + " " + str(entry.get("block_text") or "")
    suggested = entry.get("suasi_pose")
    if SENSITIVE.search(context):
        return {"pose_id": "pointing", "reason": "被害や苦痛の説明は落ち着いた指差しにする", "source": "tone_guard"}
    if isinstance(suggested, str) and suggested in POSE_NAMES:
        reason = str(entry.get("suasi_pose_reason") or "原稿の意味に合わせた図解設計")[:160]
        return {"pose_id": suggested, "reason": reason, "source": "planner"}
    # Question markers must express an unresolved question. Explanations that
    # contain 'なぜ' without asking a question stay neutral.
    if re.search(r"[?？]|(?:でしょうか|だろうか|なのか|どちらを選|判断するには|判断に迷|考えてみ)(?:[。！？?]|$|る)", sentence):
        return {"pose_id": "thinking", "reason": "問いや判断条件を考える場面", "source": "source_rule"}
    if re.search(r"意外[にと]|驚く(?:こと|べき)|予想に反して|予想とは逆|実は.{0,25}(?:逆|少な|多く|ではな)|surprisingly|contrary to expectation", sentence, re.I):
        return {"pose_id": "surprised", "reason": "原稿に意外性や予想との逆転が明示されている", "source": "source_rule"}
    if re.search(r"まとめると|要するに|全体像|整理すると|このように|ポイントは|結論(?:は|として)|手順は|in summary|overall process", sentence, re.I):
        return {"pose_id": "explaining", "reason": "全体や要点を整理する場面", "source": "source_rule"}
    return {"pose_id": "pointing", "reason": "仕組み・関係・比較を落ち着いて説明する", "source": "source_rule"}
