# references — キャラ固定用の参照画像

ここに置いた画像を「キャラ固定（character lock）」の参照として使います。
チャンネル設定（`channels.json` の各チャンネル `defaults.character_ref`）で
参照画像のパスを指定します（このフォルダからの相対パス＝プロジェクト基準）。

## 現在の設定

- `keizai_professor.png` … 経済探求ラボの先生（教授）キャラ
  - `channels.json` の `keizai` チャンネルで
    `"character_ref": "references/keizai_professor.png"` と指定済み

## 仕組み

1. プロンプター（Claude）が各シーンに `character`(true/false) を付与
   - 先生／教授／解説役が**実際に描かれる** illustration のみ true
   - 図表(diagram/chart)・写真(realphoto)・地図(map)・装飾・人物のいないシーンは false
2. `character=true` のシーンだけ、生成時にこの参照画像を渡す
   - **gpt-image**: OpenAI の画像編集API（images.edit）で参照画像のキャラ・絵柄を反映
   - **nanobanana(Gemini)**: 参照画像＋テキストのマルチモーダル入力で反映
3. 参照ファイルが無い場合はテキスト記述方式に自動フォールバック（壊れない）
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
