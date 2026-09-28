# 本番更新

スタッフ用URLは `https://sentence.apprendre.jp`。サーバーの契約アカウント名に依存しない入口を使用する。

本番の通常ブランチは `main`。Render の Auto-Deploy は **Off**（2026-09-28 10:45 に切り替え・社長承認）。main への push は、
本番の見張り（`deploy_guard.check_upstream`）が生成の合間に Deploy Hook で配置する（下の「新しいコミットの配置」）。
GitHub Actions で認証・保存済み結果のダウンロード・再開・更新ガードを検査し、
Render のビルドでも同じテストを実行する。API生成やCLI呼び出しは行わない。

## Render 設定

- Build: `pip install -r requirements.txt pytest==8.4.2 && python scripts/check_production.py`
- Pre-deploy: `python deploy_guard.py`
- Start: `gunicorn 'migration_entry:create_app()' --bind 0.0.0.0:$PORT --timeout 600 --workers 1 --threads 8 --worker-tmp-dir /dev/shm`
- Python: 3.14.3、Starter、既存の10GBディスク `/data`、`DATA_DIR=/data`
- `MIGRATION_ACCESS=active`、既存の `APP_PASSWORD` / `SECRET_KEY` / API設定を保持する。
- `RENDER_DEPLOY_HOOK`: このサービス自身の既存 Deploy Hook URL を Render の非公開環境変数に保存する。
  GitHub やPCには保存しない。**ref パラメータを付けない**。特定コミット用Hookは自動配置を無効にするため。
- `RENDER_EXTERNAL_URL` / `RENDER_GIT_COMMIT` / `RENDER_INSTANCE_ID` はRenderの標準環境変数を使う。
- 同時配置ポリシーは `Wait`。新規Blueprintをこの設定の代わりに適用しない。
- Auto-Deploy: **Off**。`RENDER_GIT_REPO_SLUG` / `RENDER_GIT_BRANCH`（Renderの標準環境変数）で見張る先が決まる。

## 新しいコミットの配置（2026-09-28〜）

push のたびに Render が配置を始めると、生成中は下の Pre-deploy が「保留」で終わり、Render はそれを
**Deploy failed** と数えてメールを送っていた（9/27 に6件。中身は保留で実害なし）。そこで Auto-Deploy を切り、
本番のサーバー自身が次のように配置する。

1. 1分ごとに GitHub の公開リポジトリの `main` の最新コミットを読む（`info/refs`。鍵なし・REST APIの回数制限を使わない）。
2. 動いている版（`RENDER_GIT_COMMIT`）と違うコミットを見つけたら、**2分**待つ（続けて push されたら最後の1回だけ配置）。
3. 生成中・順番待ちがなく、配置の途中でもないときだけ `RENDER_DEPLOY_HOOK` を呼ぶ。生成中は呼ばずに待つ
   （待つ間も受付は止めない）。push から本番に入るまでは、空いていれば約4〜5分。
4. 同じコミットを自動で呼ぶのは1回だけ。一度動いた版にも呼ばない（`/data/.deploy-watch.json`）。
   Render で古い版へ戻したとき（Rollback）に、見張りが最新へ勝手に戻さないため。直したら新しいコミットを push する。
5. Hook の失敗は3回まで。以後は Render の Manual Deploy で入れる。

Hook を呼んでから Pre-deploy までの間（ビルド約1分半）に生成が始まった場合は、従来どおり保留して、終わってから
再試行する（このときだけ保留の失敗メールが1通出る）。本物の失敗（ビルド・テストの失敗など）は今までどおり届く。

確認: Render の Logs に `[deploy-watch] New commit xxxxxxx found on GitHub; deployment requested while no generation is running`、
Deploys の Trigger が `Deploy Hook` になる。起動時に `[deploy-watch] Watching <repo>@main for new commits` が出る。

## 生成中の更新

1. テストを通ったビルドだけが、既存SECRET_KEYで署名した停止準備リクエストを現在のサーバーに送る。
2. 新規生成・再生成・その他の変更受付を一時停止。閲覧・ログイン・ダウンロードは継続できる。
3. 実行中の変更リクエスト、背景の生成処理、queued/runningのジョブがなくなるまで待つ。
4. 既に完了していれば即配置。まだ実行中なら、この配置試行は**保留のため失敗表示**で終了する
   （Auto-Deploy を切った後は、Manual Deploy・環境変数の変更などでだけ起こる）。
   サーバー側が待機を継続し、完了後15秒以内（初回依頼後90秒以降）にHookで配置を自動再試行する。
   Renderの30分制限や待機中のpipeline minutes消費を避けるため、Render内で長時間sleepしない。
5. 再試行のビルドにも同じテストをかけ、最新の成功したコードに切り替える。
   配置中に新しい生成が入らないよう受付停止を永続化する。
6. 期待するコミットの新インスタンスが起動したときだけ受付を再開する。

処理ファイルの読取り失敗・不明状態は「実行中」と扱う。再生成はジョブのcompleted状態だけで
判定せず、HTTP処理の終了まで待つ。背景生成は完了状態を書いた後の最終保存まで保護する。
この仕組みは**1 worker**専用。workersやインスタンス数を増やす際は共有ロックへ変更が必要。

## 失敗・運用上の注意

- Hookの通信失敗は最大3回だけ再試行する。秘密値をログに出さない。
- ビルドや新インスタンス起動が失敗した場合、受付停止を勝手に解除しない。
  Renderの通知・Deploysを確認し、修正をmainへpushして再配置する。
- 更新を中止するときは、Renderで進行中・待機中の配置を全てキャンセルしたことを確認してから、
  Shellで `/data/.deploy-guard.json` を退避する。配置が走っている間に解除しない。
- 手動Restart、ディスク/環境変数の変更、サービスの停止は、このPre-deployを迂回する場合がある。
  先に `/data/.migration-readonly` を作り、進行中ジョブと変更リクエストの完了を確認する。
- Auto-Deploy を再びオンにすると、push のたびの保留の失敗メールが戻る。見張りは Auto-Deploy がオンでも
  Pre-deploy が来ている間は何もしないが、ビルドが失敗したコミットには2分後にもう1回だけ Hook を呼ぶ。
- 初回導入は既存サービスを受付停止し、実行中0件を確認した上で行う。
  このコードとHook設定が本番へ入るまでは自動配置を有効にしない。
- 旧darumaの受付停止・バックアップは、確認期間終了まで維持する。
- 通常のGitHub編集権限でコードを修正できる。Renderの契約管理権限は別で、スタッフへの
  共有ログインやAPIキーの受渡しは不要。
