# 2026-09-24 Render移設用の入口

**移設完了後の通常運用は [DEPLOYMENT.md](DEPLOYMENT.md) を参照。**
以下は初回復元・切替時の手順であり、通常更新を移行ブランチへ固定する指示ではない。

本番 `02c95306116bf467ae57a1354c0ae08494f14821` から分岐した移行専用ブランチ。
画像モデル、原稿処理、チャンネル設定、保存済みデータの仕様は変更していない。

新サービスは手動作成し、Python 3.14.3、既存の requirements.txt、Starter、
永続ディスク10GB（/data）を使用する案。既存 render.yaml の Standard 指定は
今回の見積構成と異なるため、そのまま Blueprint を新規適用しない。
自動デプロイを停止し、このブランチの確認済みコミットを指定する。

起動コマンド:

```sh
gunicorn 'migration_entry:create_app()' --bind 0.0.0.0:$PORT --timeout 600 --workers 1 --threads 8 --worker-tmp-dir /dev/shm
```

APP_PASSWORD、SECRET_KEY、ツール別・チャンネル別APIキーを元サービスから保持し、
DATA_DIR=/dataを設定する。認証設定が空なら起動を拒否する。
MIGRATION_ACCESS 未指定時は readonly。閲覧・通常ログイン・ダウンロードは可能で、
生成・再生成・再開・フィードバック・削除等の変更リクエストは503で停止する。
新規の変更ルートも原則停止対象になる。

データの全件照合、認証、CSV/ZIP、実生成の移行先検証が終わり、旧側の受付停止と
処理完了・最終バックアップを確認してから、新側だけ MIGRATION_ACCESS=active にする。
検証中の実生成を許可するときも旧側との同時受付を避け、試験専用ジョブを使う。

activeでも /data/.migration-readonly があれば変更リクエストを止められる。
このフラグは通過済みの実行中処理をキャンセルしない。停止後はジョブの終了と
外部キューの処理を確認してからバックアップ・切替を行う。
ディスクが読めない場合も変更リクエストを拒否する。

このファイル・コードを準備しただけでは新Renderの起動や本番切替の完了ではない。
最新の進捗・費用決裁・バックアップ記録はAI_Workspaceの
docs/render-sentence-migration-test-2026-09-24.mdを参照する。
