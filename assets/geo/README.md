# assets/geo — 地図データ（v3 Step2）

ロシアの素材選びで手動の「地図」を押したときは、図解スタジオの
`geodata/countries.json` と同じ `russia-countries.json` を使う。
Natural Earth 1:10m Admin 0 Countries（日本の立場版・パブリックドメイン）を出典に、
図解スタジオで南樺太と千島列島の帰属未定部分を分けたデータ。
2026-11-24 の移植元は zukai-tool `4c19cc0`。データの SHA-256 は
`a01804909d67a94ea2b49304715e7658ab28c487facdc725d91605b76497281d`。
自動生成用の既存データは下記のファイルを使う。

`renderer.py` の map 描画に使う国境データ。

## ファイル
- `ne_50m_admin_0_countries.geojson` … Natural Earth 1:50m Admin 0 Countries
  - 出典: Natural Earth (https://www.naturalearthdata.com/) / GeoJSON 版は
    nvkelso/natural-earth-vector より取得。
  - **ライセンス: パブリックドメイン**（クレジット不要・商用利用可）。
  - 国コードは `ISO_A3`（欠番 -99 は `ADM0_A3` / `ISO_A3_EH` で補完）、
    日本語国名は `NAME_JA` を使用。

## 国境表現について（重要・人間判断事項）
- 本データは **Natural Earth 標準の国境**をそのまま使用する。
- クリミア半島・係争地等の表現は Natural Earth 準拠であり、**編集方針として
  変更したい場合は、このファイル（GeoJSON）を差し替える**こと。
  コードは ISO3 で国を引くだけなので、地物を編集すれば描画も変わる。
- すなわち「どの国境を正とするか」はチャンネルの編集判断に委ねる設計。

## 日付変更線（ロシア等）
- ロシアのように経度 180°をまたぐ国は、`world` extent で描画の暴れが出る場合が
  ある。地政学チャンネルで頻出の `europe` / `former_ussr` / `asia` extent では
  問題ない。ラベル・矢印は表示範囲に交差した可視部分へ配置している。
