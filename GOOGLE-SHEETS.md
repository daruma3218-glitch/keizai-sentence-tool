# 画像とコメント列つきのGoogleスプレッドシート出力

## 使い方

制作の進捗・候補の比較画面にある「Googleスプレッドシートに出す」から、保存先のGoogleアカウントを選ぶ。マイドライブに新しいGoogleスプレッドシートを作り、成功後の「作成したシートを開く」で確認する。黄色のコメント列に記入する。

- 全場面を番号順に出力する。採用済み画像を優先し、未採用の候補・スキップ・失敗も状態を明記して残す。前の素材の継続は同じ章の範囲だけで解決する。
- 画像は小さいプレビューをファイル内に埋め込む。元画像を公開する必要はない。画像URLはログインが必要な素材スタジオへの参照として併記する。
- 毎回新規作成。既存シートのコメントを上書きしない。生成結果・採用・表示設定にも書き込まない。シート上のコメントの自動取り込みは行わない。
- 画像が壊れている場合は、その場面のセルに警告を残す。別の画像へ黙って差し替えない。
- Googleへの保存結果が不明な場合は自動再送しない。Driveの「素材レビュー」を確認してから再操作する。

## Google連携の設定

設定前は直接出力ボタンを無効にし、準備中と表示する。CSVは利用できる。

1. Google Cloudで素材スタジオ専用のOAuthウェブクライアントを作成する。既存の生成用APIキーやデスクトップ用OAuthクライアントを転用しない。
2. 承認済みJavaScript生成元を `https://sozai.apprendre.jp` に限定する。ブラウザーのGIS token modelを使用し、リダイレクト先やクライアントシークレットは使用しない。
3. OAuth同意画面のアプリ名を素材スタジオにし、運用するスタッフのアカウントに対応する公開対象・テストユーザーを設定する。新しいアクセス権を作る操作は管理者の承認後に行う。
4. Drive APIを有効にする。要求するスコープは `https://www.googleapis.com/auth/drive.file` のみ。アプリが作るファイルに限定し、Drive全体へのアクセスは要求しない。
5. 公開クライアントIDをRenderの `GOOGLE_SHEETS_CLIENT_ID` に設定する。設定更新時も `DEPLOYMENT.md` の生成保護を守る。アクセストークン・クライアントシークレット・デスクトップの認証ファイルをRenderへ置かない。
6. 本番の画面からGoogle認証→画像付きシート作成→画像とコメント欄の確認まで検証する。テストユーザーだけでの確認とスタッフ全員への提供を区別する。

ブラウザーがGoogleから短時間のアクセストークンを受け取り、Driveへ直接送る。トークンを素材スタジオのサーバー、URL、localStorageへ保存しない。Googleへのアップロードはresumable uploadで、XLSXをネイティブGoogle Sheets形式へ変換する。作成後に形式と編集URLを読み戻して確認する。シートの共有は作成者がGoogle側で選ぶ。

## CSVとスキップ行

- 「コメント欄つきCSV」は画像URL・状態・要修正メモ・コメントを含む全場面の一覧。CSVには画像自体を埋め込めない。
- 既存CSVは元の列を先頭に維持し、画像URL・生成／採用状態・要修正メモ・コメントを追加する。保存済みの元CSVは変更しない。ZIP内のCSVにも反映する。
- 採用素材ZIPのCSVには要修正メモ・コメントを追加する。
- スキップ・間引き・上限スキップ・前素材の継続・素材なしの行に「画像を生成」を表示する。一括生成中／待機中は表示しない。料金の案内と任意の画像指示を確認してから、既存の個別生成処理で候補を作る。採用画像や他の行は変更しない。

## 検証

`python scripts/check_production.py` で認証・CSRF・全行の保持・継続画像・画像破損・採用不変・数式としての誤解釈防止・既存CSVの列保持・ZIP・スキップ行からの生成・描画済み進捗ページのJavaScript構文を検査する。生成APIとGoogle通信はスタブで、課金生成を行わない。

2026-10-23、同じ出力処理で作成した架空データのXLSXをGoogle Driveのネイティブ変換で取り込み、Chromeで埋め込みプレビューと黄色のコメント列を確認した。これは変換形式の検証であり、本番OAuthクライアントを使う一連の操作の検証とは別。

公式仕様: [Google Identity Services token model](https://developers.google.com/identity/oauth2/web/guides/use-token-model)、[Drive uploads and conversion](https://developers.google.com/workspace/drive/api/guides/manage-uploads)、[Drive scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)。
