#!/usr/bin/env python3
"""renderer.py (v3 Step1) — chart を matplotlib で決定論レンダリング（LLM不使用）

2026-06-12 安福: AI 製グラフの「数値ズレ・日本語文字化け」を構造的に排除するため新設。
原稿に書かれた数値をそのまま描画し、日本語は同梱フォントで描く。
verifier による検品（確率的）を不要にする。

入力: chart_spec（dict, §3.2）/ chart_theme（dict, channels.json）
出力: 1920x1080(16:9) PNG。成功で True、失敗で False（呼び出し側で engine:ai へ降格）。
LLM は一切使わない。
"""

import os
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # ヘッドレス（サーバ）
import matplotlib.pyplot as plt
from matplotlib import font_manager as fm

# ===== フォント登録（文字化けを構造的に排除）=====
_ASSETS = Path(__file__).parent / "assets"
_FONT_DIR = _ASSETS / "fonts"
_FONT_NAME = "Noto Sans JP"


def _register_fonts() -> bool:
    ok = False
    for fname in ("NotoSansJP-Regular.ttf", "NotoSansJP-Bold.ttf"):
        p = _FONT_DIR / fname
        if p.exists():
            try:
                fm.fontManager.addfont(str(p))
                ok = True
            except Exception:
                pass
    if ok:
        matplotlib.rcParams["font.family"] = _FONT_NAME
    matplotlib.rcParams["axes.unicode_minus"] = False  # マイナス記号の豆腐対策
    return ok


FONTS_AVAILABLE = _register_fonts()

# ===== 既定テーマ（channels.json の chart_theme で上書き）=====
DEFAULT_THEME = {
    "bg": "#FFFDF7", "main": "#1E40AF", "accent": "#C2410C",
    "grid": "#E5E7EB", "text": "#1F2937", "font": "NotoSansJP",
}

# 出力サイズ: 19.2 x 10.8 inch * 100dpi = 1920x1080
_W_IN, _H_IN, _DPI = 19.2, 10.8, 100

VALID_CHART_TYPES = ("bar", "line", "pie", "big_number", "comparison", "timeline")


# ===== 数値フォーマット =====
def _fmt_num(v) -> str:
    """数値を見やすく整形（整数はカンマ区切り、小数は無駄な0を落とす）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f == int(f):
        return f"{int(f):,}"
    s = f"{f:,.2f}".rstrip("0").rstrip(".")
    return s


def _fmt_val(v, unit: str = "") -> str:
    s = _fmt_num(v)
    return f"{s}{unit}" if unit else s


def _series(spec: dict):
    """series を [(label, value)] に正規化。value は数値化できるものだけ。"""
    out = []
    for it in (spec.get("series") or []):
        if not isinstance(it, dict):
            continue
        out.append((str(it.get("label", "")), it.get("value")))
    return out


def _num_series(spec: dict):
    """数値 series のみ [(label, float)]。数値化できない要素は除外。"""
    out = []
    for label, val in _series(spec):
        try:
            out.append((label, float(val)))
        except (TypeError, ValueError):
            continue
    return out


# ===== 共通の装飾（タイトル・出典）=====
def _draw_title_and_source(fig, spec: dict, theme: dict):
    title = (spec.get("title") or "").strip()
    if title:
        fig.text(0.5, 0.93, title, ha="center", va="top",
                 fontsize=46, fontweight="bold", color=theme["text"])
    src = (spec.get("source_note") or "").strip()
    if src:
        fig.text(0.98, 0.03, f"出典: {src}", ha="right", va="bottom",
                 fontsize=20, color="#6B7280")


def _palette(theme: dict, n: int):
    """main/accent を基点に n 色のパレットを作る（虹色は使わない）。"""
    base = [theme["main"], theme["accent"], "#0F766E", "#7C3AED", "#B45309", "#475569"]
    if n <= len(base):
        return base[:n]
    # 足りなければ薄い繰り返し
    return [base[i % len(base)] for i in range(n)]


# ===== 各 chart_type の描画 =====
def _draw_bar(fig, spec: dict, theme: dict):
    data = _num_series(spec)
    if not data:
        raise ValueError("bar: 数値 series が空")
    labels = [d[0] for d in data]
    values = [d[1] for d in data]
    unit = spec.get("unit", "")
    hi = spec.get("highlight_index")
    ax = fig.add_axes([0.10, 0.13, 0.84, 0.68])
    ax.set_facecolor(theme["bg"])
    colors = [theme["main"]] * len(values)
    if isinstance(hi, int) and 0 <= hi < len(values):
        colors[hi] = theme["accent"]
    bars = ax.bar(range(len(values)), values, color=colors, width=0.62, zorder=3)
    ax.set_xticks(range(len(values)))
    ax.set_xticklabels(labels, fontsize=28, color=theme["text"])
    ax.tick_params(axis="y", labelsize=22, colors="#6B7280")
    ax.grid(axis="y", color=theme["grid"], linewidth=1, zorder=0)
    for sp in ("top", "right", "left"):
        ax.spines[sp].set_visible(False)
    ax.spines["bottom"].set_color(theme["grid"])
    # 値ラベル（軸を読ませない）
    top = max(values) if values else 1
    for i, b in enumerate(bars):
        ax.text(b.get_x() + b.get_width() / 2, b.get_height() + top * 0.02,
                _fmt_val(values[i], unit), ha="center", va="bottom",
                fontsize=28, fontweight="bold", color=theme["text"])
    ax.set_ylim(0, top * 1.18)


def _draw_line(fig, spec: dict, theme: dict):
    data = _num_series(spec)
    if not data:
        raise ValueError("line: 数値 series が空")
    labels = [d[0] for d in data]
    values = [d[1] for d in data]
    unit = spec.get("unit", "")
    ax = fig.add_axes([0.10, 0.13, 0.84, 0.68])
    ax.set_facecolor(theme["bg"])
    ax.plot(range(len(values)), values, color=theme["main"], linewidth=4,
            marker="o", markersize=12, markerfacecolor=theme["accent"],
            markeredgecolor="white", markeredgewidth=2, zorder=3)
    ax.set_xticks(range(len(values)))
    ax.set_xticklabels(labels, fontsize=26, color=theme["text"])
    ax.tick_params(axis="y", labelsize=22, colors="#6B7280")
    ax.grid(axis="y", color=theme["grid"], linewidth=1, zorder=0)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    for sp in ("bottom", "left"):
        ax.spines[sp].set_color(theme["grid"])
    rng = (max(values) - min(values)) or max(abs(max(values)), 1)
    for i, v in enumerate(values):
        ax.text(i, v + rng * 0.04, _fmt_val(v, unit), ha="center", va="bottom",
                fontsize=24, fontweight="bold", color=theme["text"])
    ax.set_ylim(min(values) - rng * 0.12, max(values) + rng * 0.20)


def _draw_pie(fig, spec: dict, theme: dict):
    data = _num_series(spec)
    if not data:
        raise ValueError("pie: 数値 series が空")
    labels = [d[0] for d in data]
    values = [abs(d[1]) for d in data]
    hi = spec.get("highlight_index")
    explode = [0.06 if (isinstance(hi, int) and i == hi) else 0 for i in range(len(values))]
    ax = fig.add_axes([0.18, 0.10, 0.64, 0.70])
    colors = _palette(theme, len(values))
    wedges, _txts, autotxts = ax.pie(
        values, labels=labels, explode=explode, colors=colors,
        autopct=lambda p: f"{p:.0f}%", startangle=90, counterclock=False,
        textprops={"fontsize": 28, "color": theme["text"]},
        wedgeprops={"edgecolor": theme["bg"], "linewidth": 3},
    )
    for at in autotxts:
        at.set_color("white")
        at.set_fontsize(26)
        at.set_fontweight("bold")
    ax.set_aspect("equal")


def _draw_big_number(fig, spec: dict, theme: dict):
    """巨大数字 + 単位 + ラベル（テロップ的な説明画面）。"""
    data = _series(spec)
    # 数値を1つ取り出す（series[0] または spec['value']）
    val = None
    label = ""
    if data:
        label, val = data[0]
    if val is None:
        val = spec.get("value")
    if val is None:
        raise ValueError("big_number: 値がない")
    unit = spec.get("unit", "")
    fig.text(0.5, 0.50, _fmt_num(val), ha="center", va="center",
             fontsize=260, fontweight="bold", color=theme["main"])
    if unit:
        fig.text(0.5, 0.30, unit, ha="center", va="center",
                 fontsize=64, fontweight="bold", color=theme["accent"])
    if label:
        fig.text(0.5, 0.20, label, ha="center", va="center",
                 fontsize=46, color=theme["text"])


def _draw_comparison(fig, spec: dict, theme: dict):
    """2値（以上）を巨大数字で横並び比較（A vs B）。"""
    data = _series(spec)
    if len(data) < 2:
        raise ValueError("comparison: 2つ以上の値が必要")
    data = data[:3]  # 最大3
    unit = spec.get("unit", "")
    hi = spec.get("highlight_index")
    n = len(data)
    slot = 1.0 / n
    for i, (label, val) in enumerate(data):
        cx = slot * (i + 0.5)
        color = theme["accent"] if (isinstance(hi, int) and i == hi) else theme["main"]
        fig.text(cx, 0.52, _fmt_val(val, unit), ha="center", va="center",
                 fontsize=150, fontweight="bold", color=color)
        fig.text(cx, 0.30, label, ha="center", va="center",
                 fontsize=44, color=theme["text"])
        if i < n - 1:
            fig.text(slot * (i + 1), 0.50, "vs", ha="center", va="center",
                     fontsize=56, color="#9CA3AF")


def _draw_timeline(fig, spec: dict, theme: dict):
    """横一直線のタイムライン。series=[{label: 時点, value: 出来事(文字 or 数値)}]。"""
    data = _series(spec)
    if not data:
        raise ValueError("timeline: series が空")
    data = data[:6]
    ax = fig.add_axes([0.06, 0.18, 0.88, 0.58])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.plot([0.04, 0.96], [0.5, 0.5], color=theme["grid"], linewidth=4, zorder=1)
    n = len(data)
    xs = [0.08 + (0.84) * (i / max(1, n - 1)) for i in range(n)]
    for i, (label, val) in enumerate(data):
        x = xs[i]
        ax.scatter([x], [0.5], s=420, color=theme["accent"], zorder=3,
                   edgecolors="white", linewidths=3)
        up = (i % 2 == 0)
        ty = 0.72 if up else 0.28
        ax.text(x, 0.5 + (0.10 if up else -0.10), str(label), ha="center",
                va=("bottom" if up else "top"), fontsize=30,
                fontweight="bold", color=theme["main"])
        ax.text(x, ty, str(val), ha="center", va=("bottom" if up else "top"),
                fontsize=26, color=theme["text"], wrap=True)


_DRAWERS = {
    "bar": _draw_bar, "line": _draw_line, "pie": _draw_pie,
    "big_number": _draw_big_number, "comparison": _draw_comparison,
    "timeline": _draw_timeline,
}


def render_chart(spec: dict, output_path, theme: dict = None) -> bool:
    """chart_spec を 1920x1080 PNG に描画する。

    成功で True。spec 不正・描画失敗時は False を返し、図は作らない
    （呼び出し側で engine:ai の従来ルートへ降格すること）。
    """
    if not isinstance(spec, dict):
        return False
    theme = {**DEFAULT_THEME, **(theme or {})}
    ctype = spec.get("chart_type", "bar")
    drawer = _DRAWERS.get(ctype, _draw_bar)
    fig = None
    try:
        fig = plt.figure(figsize=(_W_IN, _H_IN), dpi=_DPI)
        fig.patch.set_facecolor(theme["bg"])
        drawer(fig, spec, theme)
        _draw_title_and_source(fig, spec, theme)
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(str(output_path), dpi=_DPI, facecolor=theme["bg"])
        return True
    except Exception as e:
        print(f"  [renderer ERROR] chart_type={ctype}: {str(e)[:140]}", flush=True)
        return False
    finally:
        if fig is not None:
            plt.close(fig)
        else:
            plt.close("all")
