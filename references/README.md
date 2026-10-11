# references — キャラ固定用の参照画像

ここに置いた画像を「キャラ固定（character lock）」の参照として使います。
チャンネル設定（`channels.json` の各チャンネル `defaults.character_ref`）で
参照画像のパスを指定します（このフォルダからの相対パス＝プロジェクト基準）。

## 現在の設定

- 大人の学び直しTVの「すあし社長の図解解説（B）」は、`suasi-poses/` の指差し・考える・驚く・両手で説明を、内容に応じて生成後に小さく配置する。画像AIに毎回キャラを描き直させない。社長の反映指示・SHAは `suasi-poses/catalog.json`。指差しは「腕が長い」という指摘を受けた修正版。新作の顔と体型は目視で検品し、実行時は登録画像とのSHA一致を確認する。
- 顔・体型の基準は引き続き `suasi-good-original.png`。元は `I:/JASPER2nd/プロフィールやロゴなど/すあし社長　グッド.png` の既存透過版（`outputs/すあし社長アカデミー/ロゴ/20260928_v2/character_cutout.png`）。不透明565,399画素のRGBは原画と完全一致。ファイルSHA-256は `eaf4886892f21ed067fa8700ed7aaa72480e3a22f68e021f44fb3f5c023c77ee`。原画または4ポーズの欠落・改変時は生成前に停止する。
- 10/11の最初のBでは、盾を持つアイコンを参照してAIに描き直させたため、社長から顔・体型が違うと指摘された。元のアイコンは入口用として保持し、B生成は上記の原画配置へ修正した。旧B案件の追加生成・手直しにも適用し、保存済み候補は自動上書きしない。詳細は `docs/otona-style-study-2026-10-11.md`。

- `keizai_professor.png` … 経済探求ラボの先生（教授）キャラ
  - `channels.json` の `keizai` チャンネルで
    `"character_ref": "references/keizai_professor.png"` と指定済み

## 仕組み

1. プロンプター（Claude）が各シーンに `character`(true/false) を付与
   - 先生／教授／解説役が**実際に描かれる** illustration のみ true
   - 図表(diagram/chart)・写真(realphoto)・地図(map)・装飾・人物のいないシーンは false
2. `character=true` に加え、世界観ロック中の図解などへも生成時に参照画像を渡す
   - **gpt-image**: OpenAI の画像編集API（images.edit）で参照画像のキャラ・絵柄を反映
   - **nanobanana(Gemini)**: 参照画像＋テキストのマルチモーダル入力で反映
3. 従来設定では参照ファイルが無い場合はテキスト記述方式にフォールバック。すあし社長Bは上記の別工程で原画を配置し、原画が無ければ停止する
   - 「世界観統一モード」ON のときのみ有効

## 別チャンネルでキャラ固定したいとき

1. キャラ画像（正方形 PNG 推奨・顔と服がはっきり分かるもの）をこのフォルダに保存
   例: `references/<channel>_character.png`
2. `channels.json` の該当チャンネル `defaults` に
   `"character_ref": "references/<channel>_character.png"` を追加
3. デプロイ（この画像はリポジトリにコミットされ Render にも反映される）

## 注意

- 参照画像はリポジトリにコミットして良い（機密ではない）。
- `.env` / `.secret_key` など機密ファイルは引き続きコミット禁止。
