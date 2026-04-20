プロジェクト管理表

## 直近状態（2026-04-19 更新）

- **ブランチ**: `develop` に変更反映済み（`origin/develop` と同期）。**回帰確認**（下記チェックリスト相当）後、問題なければ `develop` → `master` の PR で安定版へマージする想定。
- **Issue #6**（SSM String 化 / KMS コスト）: **`ID:001` は完了 `[x]`**。GitHub 上は **#6 はクローズ済み**。
- **着手前**: `docs/roadmap.md` の Step 順（利用者向け 👤 節含む）と、本条「**ステップ横断の考慮事項**」を読む。
- **機密スキャン（運用）**: `check_env_leaks` は**現在の** `.env` の値だけを履歴検索する。シークレットを**ローテーションした直後**は、**変更前の値**でもう一度履歴を走らせる（`git grep` 等）か、旧値が履歴に無いことを別途確認する（ツール拡張は後回しで可）。
- **次の候補（ブランチは ID ごとに分離。039/006 と 038/033 を同一ブランチに混ぜない）**:
  - **ID:039** Executor モジュール分割（同一 Lambda）→ **ID:006** ハイブリッド → 検証後 **ID:040**
  - 並行候補: **ID:038** Discord 枠組み先行 / **ID:033** 以降 Step 3 S3 統合（ロードマップ現行フェーズと整合）

### 回帰確認チェックリスト（`develop`）

[x] 対象環境の `.env.<env>` が意図どおり（`check_env_leaks` 等で漏れがないこと）
[ ] `test_runner`（必要なら **ID:028** 相当の部分実行オプション利用）で主要疎通が通ること
[ ] Discord / Lambda ルーティングが環境サフィックス（例: `*-dev`）で誤フォールバックしていないこと

---

[x] ID:001 [FIX] [OPS] 非機密SSMをString保存へ変更
    ・関連箇所: scripts/register.py
    ・Issue: #6 (https://github.com/ky0709/factorio-server-manager/issues/6)
    ・背景: .envで管理している環境変数をSSMへアップロードしてLambda実行時に都度取得しているが、非機密情報までSecureStringになっており、復号付きリクエスト増加でコスト上昇リスクがあるため。
    ・完了条件: 非機密パラメータがStringで保存され、SecureStringは機密情報のみに限定されること。

[ ] ID:002 [TASK] [LOGIC] 無人判定を`ZERO_PLAYER_THRESHOLD`から`MIN_IDLE_MINUTES`へ移行
    ・関連箇所: scripts/register.py, scripts/check_env_leaks.py, aws/Lambda/Factorio_Worker/lambda_function.py
    ・背景: 無人判定が連続チェック回数依存だと監視間隔に影響されやすいため、経過時間ベースに統一して運用安定性を上げる必要があるため。
    ・完了条件: 3ファイルで`MIN_IDLE_MINUTES`へ統一され、`ZERO_PLAYER_THRESHOLD`依存がなくなること。

[x] ID:003 [TASK] [LOGIC] String化後の`get_parameter(WithDecryption=True)`互換性確認
    ・関連箇所: aws/Lambda/Factorio_Notifier/lambda_function.py
    ・背景: ID:001で非機密情報をStringへ切り替えた後も、Notifierの単体取得処理が既存実行フローで失敗しないことを確認する必要があるため。
    ・完了条件: `get_parameter(WithDecryption=True)`でString/SecureString混在時もエラーが発生しないこと。

[x] ID:004 [TASK] [LOGIC] String化後の`get_parameters_by_path(WithDecryption=True)`互換性確認
    ・関連箇所: aws/Lambda/factorio_common_layer/python/factorio_common/utils.py
    ・背景: ID:001で非機密情報をStringへ切り替えた後も、共通レイヤーの一括取得処理が既存実行フローで失敗しないことを確認する必要があるため。
    ・完了条件: `get_parameters_by_path(WithDecryption=True)`でString/SecureString混在時もエラーが発生しないこと。

[x] ID:005 [TASK] [OPS] EC2 Factorio環境構築とサーバーレス疎通確認
    ・関連箇所: docs/ec2_setup_reference.md
    ・背景: 2026-04-15時点でVPC/サブネット/IGW/RT/SG作成までは完了しており、Lambda等からEC2上のFactorioを実運用可能にする残作業を手順化する必要があったため。2026-04-16 に S3 Files の mount -a、Interactor→Worker-dev ルーティング、`/start` `/save` `/stop` `/restore list` `/restore select` の実機動作、および `saves/save.zip` への保存と S3 バージョニング反映を確認した。
    ・完了条件: EC2作成、初期セットアップ、Factorio/RCON/S3 Files/IAMロール設定、サーバーレス疎通確認までを再現可能な手順として文書化されていること。加えて、`/save` または `/stop` 実行後に S3 上でセーブデータ更新が確認でき、`/restore list` で取得可能になること。

[ ] ID:006 [TASK] [LOGIC] サーバー起動ロジックをSTATIC/DYNAMICのハイブリッド運用へ拡張
    ・関連箇所: aws/Lambda/Factorio_Executor/lambda_function.py, aws/Lambda/Factorio_Worker/lambda_function.py, scripts/register.py, scripts/check_env_leaks.py, .env.example, README.md, aws/IAM/FactorioExecutePolicy/policy.json.example, aws/IAM/FactorioWorkPolicy/policy.json.example
    ・背景: 固定インスタンスの起動停止運用を維持しつつ、起動テンプレート経由の新規作成（spot/ondemand）と終了を切り替え可能にし、運用コストと可用性の選択肢を持たせる必要があるため。Executor が肥大化しているため、**ID:039 で同一 Lambda 内モジュール分割を先行**し、その境界に沿ってハイブリッド分岐を載せる（巨大関数への if 積み増しを避ける）。
    ・完了条件: `SERVER_RUN_MODE`（`STATIC`/`DYNAMIC`）・`DYNAMIC_CAPACITY_MODE`（`ONDEMAND`/`SPOT`）・`INSTANCE_LIFECYCLE_MODE`（`PERSISTENT`/`EPHEMERAL`）を使った分岐方針、起動テンプレート利用時の run/terminate フロー、アクティブInstanceIdの保持先（DynamoDB等）、Spot中断時の退避方針、必要IAM権限の差分、実装着手手順（ブランチ作成開始を含む）がコード内TODOとTASKS.mdから追跡可能になっていること。**ID:039 完了後に本ロジックを実装**し、稼働検証後に **ID:040** で Lambda 物理分割の要否を判断すること。
    ・メモ（後対応）: `.env` 全項目の詳細リファレンス（例: `docs/env_reference.md`）は後続で作成する。`STATIC -> DYNAMIC` 切替時の既存インスタンス自動整理（save/logs同期後に stop/terminate）も Step 5 本実装で扱う。`SERVICE_UNIT_NAME` については、標準の Factorio 専用EC2 ではリスク低めだが、他プロセス同居サーバーでは誤ユニット停止リスクがある旨をリファレンスへ明記する。

[x] ID:007 [FIX] [LOGIC] deploy_policies.py の環境読込優先度を修正
    ・関連箇所: scripts/deploy_policies.py
    ・背景: `.env.dev` が `-dev` 命名でも、先に読み込まれた `.env` 値が優先されると誤ったポリシー名で比較され「No changes detected」となるため。
    ・完了条件: `deploy_policies.py <env>` 実行時に対象環境ファイルの値が確実に優先されること。

[x] ID:008 [FIX] [LOGIC] 各デプロイスクリプトの env 読込優先度を統一
    ・関連箇所: scripts/setup_config.py, scripts/register.py, scripts/deploy_lambda.py, scripts/update_layer.py, scripts/init_aws_resources.py, scripts/update_eventbridge.py, scripts/test_runner.py
    ・背景: 一部スクリプトで `.env` の先読みや `override=False` により、`dev` 実行時でも `prod` 値が残留するリスクがあるため。
    ・完了条件: すべての対象スクリプトで指定環境ファイルが優先される読み込み順・設定になっていること。

[x] ID:009 [FIX] [LOGIC] dev環境のロール名サフィックス重複を防止
    ・関連箇所: scripts/init_aws_resources.py, scripts/update_eventbridge.py
    ・背景: `.env.dev` 側で `-dev` を含む値に対してスクリプト側も `-dev` を付与し、`-dev-dev` になる不整合が発生したため。
    ・完了条件: 既に `-dev` が付与された値でも重複せず、期待どおりのロール名で処理されること。

[x] ID:010 [FIX] [LOGIC] update_eventbridge.py の未作成リソース処理を修正
    ・関連箇所: scripts/update_eventbridge.py
    ・背景: Schedule/Rule 未作成状態で update_eventbridge.py を実行すると、`current_rule` 未定義参照で失敗し初期化を継続できないため。
    ・完了条件: 未作成の Schedule/Rule がある状態でもスクリプトが異常終了せず、必要リソースを作成または更新できること。

[x] ID:011 [TASK] [QUAL] README に Interactor の PyNaCl レイヤー手順を明記
    ・関連箇所: README.md
    ・背景: 依存ライブラリとして PyNaCl の記載はあるが、Interactor 関数に当該レイヤーを適用する明示手順が不足し、初回構築時に見落としやすいため。
    ・完了条件: セットアップ手順内で PyNaCl レイヤー適用と確認方法が明示されていること。

[x] ID:012 [FIX] [LOGIC] Executor の instance_id 欠落時ハンドリングとテスト待機条件を修正
    ・関連箇所: aws/Lambda/Factorio_Executor/lambda_function.py, scripts/test_runner.py
    ・背景: SSM 取得失敗時に `config['instance_id']` で KeyError になり、test_runner が停止待機へ進んでハングするため原因切り分けが困難だったため。
    ・完了条件: instance_id 欠落時に明示的なエラー文を返し、test_runner が異常レスポンス時に不要な停止待機へ進まないこと。

[x] ID:013 [FIX] [SEC] IAMポリシーテンプレートの環境依存値をプレースホルダー化
    ・関連箇所: aws/IAM/*Policy/policy.json.example, scripts/setup_config.py
    ・背景: SSMパスやDynamoDBテーブル名が `/factorio/*` や `FactorioState` 固定で、dev環境でもprod向け権限が生成される不整合があったため。
    ・完了条件: `.env.<env>` の `SSM_PARAMETER_PATH` と `DYNAMODB_TABLE_NAME` が各ポリシーへ正しく反映されること。

[x] ID:014 [TASK] [OPS] 開発用EC2ロール/ポリシー（-dev）作成とインスタンス反映
    ・関連箇所: aws/IAM/FactorioServerPolicy/policy.json, docs/ec2_setup_reference.md
    ・背景: 本番と同等のEC2運用権限（SSM/CloudWatch/S3アクセス）を開発環境でも分離し、最小権限かつ環境独立で運用するため。
    ・完了条件: `init_aws_resources.py` と `deploy_policies.py` の実行で `EC2-Factorio-Server-Role-dev` / `FactorioServerPolicy-dev` が整備され、開発EC2へインスタンスプロファイル反映まで委譲できること。

[x] ID:015 [FIX] [SEC] FactorioRegistPolicy にEC2ロール/プロファイル反映権限を追加
    ・関連箇所: aws/IAM/FactorioRegistPolicy/policy.json.example, README.md
    ・背景: init_aws_resources.py で EC2 用ロール作成と instance profile 反映を行う際、iam/ec2 の必要権限不足で AccessDenied になったため。
    ・完了条件: FactorioRegistUser-dev で `init_aws_resources.py dev` 実行時に EC2 ロール/instance profile の作成・アタッチ・関連付けが継続実行できること。

[x] ID:016 [FIX] [SEC] FactorioRegistPolicy の PassRole 条件に EC2 を追加
    ・関連箇所: aws/IAM/FactorioRegistPolicy/policy.json.example
    ・背景: EC2 インスタンスプロファイルへロール追加時に `iam:PassRole` が必要だが、許可先サービスに `ec2.amazonaws.com` が含まれておらず失敗したため。
    ・完了条件: `init_aws_resources.py dev` 実行時に `AddRoleToInstanceProfile` が PassRole 条件で拒否されないこと。

[x] ID:017 [FIX] [OPS] init_aws_resources.py のIAM伝播待機と二重確認を強化
    ・関連箇所: scripts/init_aws_resources.py
    ・背景: `AddRoleToInstanceProfile` の PassRole 反映遅延で失敗しやすく、また誤実行防止の確認が本番限定だったため。
    ・完了条件: IAM反映遅延時に自動リトライし、dev/prodの両環境で二重確認を要求すること。

[x] ID:018 [FIX] [SEC] EC2サーバーロール向け PassRole 対象追加と確認タイミング調整
    ・関連箇所: aws/IAM/FactorioRegistPolicy/policy.json.example, scripts/setup_config.py, scripts/init_aws_resources.py
    ・背景: PassRole の対象Resourceに EC2 サーバーロールが含まれておらず `AddRoleToInstanceProfile` が継続失敗したこと、また二重確認が setup_config 実行後だったため。
    ・完了条件: FactorioRegistPolicy に EC2 サーバーロールが含まれ、init_aws_resources.py の二重確認が setup_config 実行前に行われること。

[x] ID:019 [TASK] [OPS] README に dev 用 API Gateway 手動作成手順を追加
    ・関連箇所: README.md
    ・背景: API Gateway は初回作成後の更新頻度が低く、スクリプト委譲より手動CLIで運用する方針のため、再現可能な実行手順を明記する必要があるため。
    ・完了条件: README に権限要件、PowerShell 実行上の注意、作成〜Lambda接続〜Discord反映までの手順が記載されていること。

[x] ID:020 [FIX] [LOGIC] Interactor 署名ヘッダ取得の堅牢化と欠落時ガード追加
    ・関連箇所: aws/Lambda/Factorio_Interactor/lambda_function.py
    ・背景: API Gateway 経由で Discord 署名ヘッダのキー表記ゆれや欠落が起きると `fromhex(None)` 例外で 401 となり、Discord 側で「アプリケーションが応答しませんでした」が発生するため。
    ・完了条件: ヘッダ名の大文字小文字差を吸収し、署名/タイムスタンプ欠落時に明示ログと安全な早期リターンで処理できること。

[x] ID:021 [FIX] [OPS] init_aws_resources に S3 Files 作成補助を追加
    ・関連箇所: scripts/init_aws_resources.py, docs/ec2_setup_reference.md, README.md
    ・背景: 現行フローでは S3 バケットまでしか自動作成されず、S3 Files の file system ID が未作成のまま EC2 マウント手順で停止しやすいため。
    ・完了条件: `S3_FILES_SYSTEM_ID` 未設定時に init スクリプトが S3 Files 作成を試行し、結果に応じた案内が表示され、関連ドキュメント手順が実装と整合していること。

[x] ID:022 [FIX] [OPS] S3 Files 自動作成に role-arn / バケットARN / RegistPolicy を反映
    ・関連箇所: scripts/init_aws_resources.py, aws/IAM/FactorioRegistPolicy/policy.json.example, scripts/setup_config.py, .env.example, docs/ec2_setup_reference.md
    ・背景: AWS CLI の `s3files create-file-system` は `--role-arn` 必須かつ `--bucket` はバケット ARN 形式のため、従来のコマンドだけでは ParamValidation で失敗するため。
    ・完了条件: init が S3 Files 用サービスロールを用意したうえで create-file-system を呼べること、および Regist ユーザーが必要権限を `deploy_policies` で取得できること。

[x] ID:023 [FIX] [UI] README に S3 Files の fileSystemId 確認と .env 反映手順を追記
    ・関連箇所: README.md
    ・背景: `init_aws_resources` 成否にかかわらず、作成済みファイルシステムの ID を CLI で確認して `.env` に書く手順が README に明示されておらず、初回構築時に迷いやすいため。
    ・完了条件: README のチェックリストおよび詳細セットアップに、`aws s3files list-file-systems` による確認と `S3_FILES_SYSTEM_ID` への反映がプレースホルダ付きで記載されていること。

[x] ID:024 [FIX] [UI] ec2_setup_reference に S3 Files マウント確立手順を追記
    ・関連箇所: docs/ec2_setup_reference.md
    ・背景: 初回構築で `mount.s3files` 未導入・マウントターゲット未作成・SG/DNS 不備により DNS 解決やマウント失敗が起きやすく、手順が README / init と分散していたため。
    ・完了条件: セクション6に依存関係の順序（FS ID → amazon-efs-utils → マウントターゲット → SG → DNS → fstab）、CLI はプレースホルダ付き、よくあるエラーと対処が記載されていること。

[x] ID:025 [FIX] [LOGIC] Interactor の SSM キー名不一致で dev ルーティングが prod 名へフォールバックする不具合を修正
    ・関連箇所: aws/Lambda/Factorio_Interactor/lambda_function.py
    ・背景: Discord 経由の実機確認で Interactor が SSM 上の `WORKER_LAMBDA_NAME` / `NOTIFIER_LAMBDA_NAME` を拾えず、`Factorio_Worker` へフォールバックして `lambda:InvokeFunction` の AccessDenied が発生したため。
    ・完了条件: Interactor が SSM から環境別 Lambda 名を正しく取得し、dev 環境で `/restore` など Worker ルーティングが `*-dev` 関数へ到達すること。

[x] ID:026 [TASK] [QUAL] test_runner の save/stop テストで S3 反映確認と後始末を自動化
    ・関連箇所: scripts/test_runner.py, aws/IAM/FactorioRegistPolicy/policy.json.example
    ・背景: 現行の統合テストは `/save` と `/stop` の応答文のみを確認しており、S3 へ実セーブが反映されたか、またテスト後に `LatestSaveInfo` とセーブオブジェクトが元状態へ戻るかを検証できていないうえ、Regist 権限に `s3:ListBucketVersions` と DynamoDB の `GetItem` / `UpdateItem` がなくベースライン取得・復元で失敗するため。
    ・完了条件: test_runner が開発環境構築フェーズでも安定実行でき、S3/DynamoDB 直接検証が不可な場合はスキップ理由を明示して主要疎通テストを継続できること。

[x] ID:027 [TASK] [LOGIC] save/stop の S3 反映確認と巻き戻しを Lambda 側完結へ移行
    ・関連箇所: aws/Lambda/Factorio_Executor/lambda_function.py, aws/Lambda/Factorio_Worker/lambda_function.py, scripts/test_runner.py, aws/IAM/FactorioExecutePolicy/policy.json.example, aws/IAM/FactorioWorkPolicy/policy.json.example
    ・背景: test_runner から S3/DynamoDB へ直接アクセスさせると Regist 権限が肥大化し、開発用ユーザーの責務分離が崩れるため、検証と巻き戻しは実行責務を持つ Lambda 側へ寄せる必要があるため。
    ・完了条件: test_runner は Lambda 呼び出しの結果判定に専念し、S3 バージョン確認・後始末は Lambda 内処理で完結すること。

[ ] ID:028 [TASK] [QUAL] test_runner に特定テストのみ実行するオプションを追加
    ・関連箇所: scripts/test_runner.py
    ・背景: ID:027 の実装・検証では save/stop 周辺だけを繰り返し試したく、毎回フルスイートを流すと時間と副作用が大きいため、対象テストだけを選択実行できるようにする必要があるため。
    ・完了条件: test_runner が test 名またはカテゴリを指定して部分実行でき、既存のフル実行フローも維持されること。

[x] ID:029 [FIX] [UI] status の LatestSaveInfo 更新時にファイルサイズも記録する
    ・関連箇所: aws/Lambda/Factorio_Executor/lambda_function.py
    ・背景: `/save` や `/stop` 実行直後は `LatestSaveInfo` に Timestamp しか入らず、`/status` 表示でサイズが `-MB` になって情報不足に見えるため。
    ・完了条件: Executor が `LatestSaveInfo` 更新時に `FileSize` も併せて保存し、`/status` でタイムスタンプとサイズが同時に表示されること。

[x] ID:030 [FIX] [UI] restore select の権限系失敗詳細をログチャットへ分離しメインチャットの案内を改善
    ・関連箇所: aws/Lambda/Factorio_Worker/lambda_function.py, aws/IAM/FactorioWorkPolicy/policy.json.example
    ・背景: `/restore select` 失敗時に AccessDenied の詳細がメインチャットへ露出し、ユーザー向け案内として冗長かつ機密寄りの情報を含むため。あわせて `SAVE_FILE_KEY` 変更後に WorkPolicy の反映が漏れると `s3:GetObjectVersion` で失敗するため。
    ・完了条件: restore select の詳細エラーはログチャットへ送られ、メインチャットには復元失敗の要約と対策のみが表示されること。必要な WorkPolicy 反映手順が追跡できること。

[ ] ID:031 [FIX] [UI] エラー時のメインチャット応答を Discord ロケールに応じて多言語化
    ・関連箇所: aws/Lambda/Factorio_Worker/lambda_function.py, aws/Lambda/Factorio_Executor/lambda_function.py, aws/Lambda/factorio_common_layer/python/factorio_common/utils.py
    ・背景: 通常メッセージは日本語/英語に対応している一方で、権限不足や設定不備など一部のエラー応答は日本語固定のハードコード文言が残っており、英語ロケール利用時に不自然なため。
    ・完了条件: メインチャット向けの主要エラー応答が `locale` に応じて日本語/英語で返ること。

[x] ID:032 [FIX] [UI] command_reference と README のコマンド仕様説明を現行実装へ整合
    ・関連箇所: docs/command_reference.md, README.md
    ・背景: `/start` `/stop` `/restore` `/pass` の説明に固定値前提や旧挙動の記載が残り、実装との差分で運用判断を誤る恐れがあるため。
    ・完了条件: command_reference と README の主要コマンド説明が現行 Lambda 実装と矛盾しない記載へ更新されていること。

[ ] ID:033 [FEAT] [OPS] mods/config/logs の S3 ディレクトリ統合を実装
    ・関連箇所: aws/Lambda/Factorio_Executor/lambda_function.py, docs/ec2_setup_reference.md, scripts/init_aws_resources.py, .env.example
    ・背景: 現在は `saves/save.zip` の運用は確立している一方、`/mods` `/config` `/logs` は S3 統合が未完了で、完全ステートレス運用が成立していないため。
    ・設計方針（着手時・バケットキー）: 本番と開発で **S3 バケットを分ける**（バケット名は `.env` の `S3_BUCKET_NAME`、公開ドキュメントではプレースホルダ）。バケット内のオブジェクトキーは **ルート直下**に `saves/`（既存の `SAVE_FILE_KEY=saves/save.zip` と整合）, `logs/`, `mods/`, `config/` を置く。`init_aws_resources.py` はバケット作成または既存確認の直後に、上記4プレフィックスを `saves/` と同様に空キーで確保する（冪等）。運用方針として `saves/mods/config` は S3 Files 側を常時参照し、`logs` はローカル出力（`/opt/factorio/logs`）を正として停止時に S3 へ同期する。初回のローカル→マウント先コピーで `rsync -a` が `chgrp` / `mkstemp` で失敗する場合は **`docs/ec2_setup_reference.md` の 6-6-1**（`--no-group`・`--temp-dir=/tmp` 等）を参照。
    ・完了条件: 上記キー構成に沿って `saves/mods/config` が S3 側の正として運用され、`logs` は停止時同期で S3 に集約されること。手順・実装・環境変数が追跡可能で、再起動・インスタンス再作成後も同一データを継続利用できること。

[ ] ID:034 [FEAT] [OPS] EC2 起動/停止オーケストレーション（save->logs同期->stop）を自動化
    ・関連箇所: docs/ec2_setup_reference.md, scripts/init_aws_resources.py, aws/Lambda/Factorio_Executor/lambda_function.py
    ・背景: S3 Files の手動確認・補正手順が残っており、インスタンス再作成や初回起動時の運用負荷が高いため。
    ・完了条件: 起動時に必要なマウントとリンク設定が自動で安定適用され、手動介入なしでゲーム実行パスが揃うこと。停止フローで `save` → `logs` の S3 同期 → EC2 停止の順序を強制し、AMI + 起動オプション運用でもログ退避漏れを防げること。`SERVICE_UNIT_NAME` を `.env` / SSM 経由で指定し、停止対象ユニットを環境ごとに明示管理できること。移行完了後に未使用となる `/mnt/factorio-data/logs` の削除（または空ディレクトリ維持の明示判断）を実施し、消し忘れを防ぐこと。

[ ] ID:035 [FEAT] [UI] Discord `/config` コマンドで server-settings を管理
    ・関連箇所: scripts/register.py, aws/Lambda/Factorio_Interactor/lambda_function.py, aws/Lambda/Factorio_Executor/lambda_function.py, aws/Lambda/factorio_common_layer/python/factorio_common/utils.py
    ・背景: 設定変更のたびにサーバーへ直接ログインする必要があり、運用者の体験が悪いため。あわせて設定項目の意味を Discord 上で確認できる導線が必要なため。
    ・完了条件: `/config` で「設定一覧の閲覧」「項目/値指定での設定変更」「設定項目一覧と説明確認（単一項目指定含む）」が実行できること。加えて、既定で `/config` 全サブコマンドを管理者限定にする専用制御変数（`RESTRICTED_COMMAND_STRINGS` とは別）を導入し、権限制御できること。

[ ] ID:036 [FEAT] [UI] Discord `/mods` と `/admin` コマンドを追加
    ・関連箇所: scripts/register.py, aws/Lambda/Factorio_Interactor/lambda_function.py, aws/Lambda/Factorio_Worker/lambda_function.py, scripts/upload_mod_assets.py
    ・背景: MOD有効化や管理者リスト編集が手動ファイル編集依存で、リモート運用性が不足しているため。MODのメタ情報確認もゲーム外で完結させたいため。
    ・完了条件: `/mods` で「有効/無効込み一覧」「有効のみ/無効のみフィルタ」「有効化/無効化」「MOD名指定で情報参照（S3上のテキスト情報。未登録時はその旨を返す）」ができること。加えて `/admin` でゲーム内管理者リストの確認と追加/削除ができること。運用面では `data/mod/` 配下の MOD本体・情報テキスト・管理JSONを専用スクリプトで S3 にアップロード（上書き）できること。

[ ] ID:037 [TASK] [OPS] Glue/Athena を用いたログ収集・解析基盤の導入可否を検討
    ・関連箇所: docs/roadmap.md
    ・背景: `/log` コマンド導入は未定だが、将来的なユーザーレポート提供に向けて、ログ収集・検索基盤の選定を先に行う必要があるため。
    ・完了条件: Glue/Athena を使ったログ収集・分析案の実現性、概算コスト、運用負荷、Discord連携方式（コマンド化有無を含む）を整理した方針が確定していること。

[ ] ID:038 [TASK] [UI] Discord リモート管理コマンドの枠組みを先行整備
    ・関連箇所: scripts/register.py, aws/Lambda/Factorio_Interactor/lambda_function.py, docs/command_reference.md, .env.example
    ・背景: `/config` `/mods` `/admin` の本実装前に、コマンド登録・ルーティング・権限制御の枠組みのみ先行整備し、運用モード拡張（ID:006）を優先実装できる状態にしたいため。
    ・完了条件: 対象コマンドの定義と安全な暫定応答（maintenance/案内）が動作し、`RESTRICTED_COMMAND_STRINGS` とは別の強制管理者制御変数（例: `STRICT_ADMIN_COMMAND_STRINGS`）で実行制御できること。

[ ] ID:039 [TASK] [QUAL] Factorio_Executor をハイブリッド実装前にモジュール分割（同一 Lambda 内）
    ・関連箇所: aws/Lambda/Factorio_Executor/lambda_function.py
    ・背景: ID:006 実装時に InstanceId 解決・起動/終了・EventBridge 連携が複雑化するため、**先に同一 Lambda 内で責務境界**（例: EC2 オーケストレーション、RCON、カタログ/DynamoDB・S3 メタ、Discord 応答整形、EB イベント処理）をファイルまたはモジュールに分割し、ハイブリッド分岐を載せる土台を作る必要があるため。
    ・完了条件: `lambda_handler` が薄くなり、主要処理がモジュール化された状態で既存フロー（STATIC）の回帰テストが通ること。分割方針が README または `docs/TASKS.md` から追跡できること。

[ ] ID:040 [TASK] [QUAL] ハイブリッド稼働検証後に Executor の Lambda 物理分割を検討
    ・関連箇所: aws/Lambda/Factorio_Executor/, scripts/deploy_lambda.py, scripts/init_aws_resources.py, aws/IAM/*Policy/policy.json.example
    ・背景: ID:006 完了後、負荷・タイムアウト・デプロイ境界の観点から、同一 Lambda 分割が妥当か判断する必要があるため。
    ・完了条件: 物理分割の要否、分割案（関数単位・責務）、IAM/EventBridge/Interactor ルーティング変更、移行手順が文書化され、実施する場合は別タスクに切り出せること。

[x] ID:041 [TASK] [OPS] ステップ横断の考慮事項を TASKS.md に集約する
    ・関連箇所: docs/TASKS.md, docs/roadmap.md
    ・背景: ロードマップ上は別ステップでも、着手順を誤ると再作業や権限・テスト不足が重なるため、開発者向けの前提を単一箇所にまとめる必要があるため。
    ・完了条件: 本条直後の「ステップ横断の考慮事項」節が追加され、主要 ID への参照が付いていること。

[ ] ID:042 [TASK] [LOGIC] `/stop` 同時実行ガード（ロック機構）を導入
    ・関連箇所: aws/Lambda/Factorio_Executor/lambda_function.py, docs/TASKS.md
    ・背景: `/stop` が短時間に複数回実行されると StopStartTime 更新や SSM コマンド実行が競合し、停止シーケンスの結果と通知が不整合になるリスクがあるため。
    ・完了条件: 停止処理中（ロック中）は追加 `/stop` を実行せず「停止中」応答のみ返すこと。ロック取得・解放の条件が明文化され、異常時にロックが残留しないガード（TTLまたは再取得条件）を備えること。

[x] ID:043 [FIX] [OPS] register.py のSSM同期で未変更パラメータを更新スキップ
    ・関連箇所: scripts/register.py, docs/ec2_setup_reference.md
    ・背景: `python scripts/register.py <env>` 実行時に全キーへ `PutParameter` を発行しており、値が同一でも API リクエストとバージョン増加が発生するため。
    ・完了条件: SSM の既存値と型が一致する場合は `PutParameter` をスキップし、更新件数とスキップ件数が実行ログで確認できること。併せて、停止時 logs 同期に必要な `awscli` 依存がセットアップ手順へ明記されていること。

## ステップ横断の考慮事項（開発・着手前チェック）

ロードマップの Step と無関係に増えやすい作業のメモ。**利用者向けの説明は `docs/roadmap.md` のみ**に寄せる。

- **Step 3（ID:033 / ID:034）と Step 5（ID:006）**  
  DYNAMIC で新規インスタンスを立てる場合、**起動時マウント・ユーザーデータ（ID:034）**が不安定だとゲーム用データが揃わない。006 を本番相当で進めるなら 034 の方針を先に固めるか、006 の検証環境で userdata を明示する。  
  **ID:033**（mods/config/logs の S3 化）は Executor のパス・I/O 前提に触れる。**ID:039** の分割境界と同時に設計しないと、分割直後に Executor を再度大きく動かすことになる。

- **Step 5（ID:006）と IAM / スクリプト**  
  DynamoDB・起動テンプレート・追加 API などを足すと、**`init_aws_resources.py`・`deploy_policies.py`・`policy.json.example`・`register.py`・`check_env_leaks.py`** がセットで動きがち（過去の IAM 整備タスクと同型）。「Lambda だけ」の見積もりにしない。

- **ID:002（無人停止）と ID:006**  
  インスタンス寿命が STATIC と DYNAMIC で変わる。**閾値の意味**（最終プレイヤー離脱からの分、ヘルスチェックとの関係など）を 006 側で決めてから 002 を仕上げると手戻りが減る。

- **ID:027 / ID:028 と Executor 変更**  
  起動・停止・待機パスが増えると、**統合テストの分岐・待機・後始末**が不足しがち。039 の STATIC 回帰に続け、006 用の最小シナリオを早めに決める。

- **Step 6（ID:035 / ID:036）と ID:033**  
  `/config` `/mods` は「正のデータが EC2 か S3 か」に依存する。033 の配置・同期方針が曖昧なまま Step 6 を進めると実装方針の転換コストが大きい。

- **ID:031（エラー文言 i18n）**  
  Step 4〜6 でコマンド・エラー経路が増えるほど、**未対応のハードコード文言が並行で積み上がる**。枠組み先行（ID:038）の段階から方針だけ決めておくとよい。

- **`factorio_common` レイヤー**  
  共通ユーティリティを変えると **複数 Lambda の再デプロイ・互換**が一括で必要になりやすい。

- **`scripts/register.py`**  
  Discord コマンド・環境変数が増えるたびに衝突しやすい。**同一ファイルを触るブランチは短く切る**か、マージ順を決める。

・運用ルール: 未完了は[ ]、完了後は[x]に更新する。主要 Step 着手前に「ステップ横断の考慮事項」節を確認する。

・ブランチ（推奨・開発者向け）
    ・既定ブランチ（例: `develop`）へ集約する前に、対象 ID の変更は **feature ブランチ**（例: `feature/ID-039-executor-split`）で完結させる。
    ・**Executor（039/006）と Discord 登録（038）や S3 統合（033）を同一ブランチに混ぜない**（`register.py` / Executor の競合とレビュー負荷を避ける）。
    ・マージ前に **対象環境で `test_runner`（必要なら ID:028 の部分実行）** を通す。インフラ変更は **dev** で確認してから prod 用ポリシー・手順を更新する。
    ・ロードマップの **大きな順序変更**をしたら、本条「ステップ横断の考慮事項」と `docs/roadmap.md` を同じ PR か直後のコミットで整合させる。

・アクション (Action)
    ・FEAT: 新機能の追加、ドキュメントの新規作成
    ・TASK: 予定された作業（改善、テスト、ライブラリ更新、雑用）
    ・FIX: 不具合修正、既存ドキュメントの修正
    ・RISK: 負の遺産、技術負債、放置すると危険な箇所

・属性 (Scope)
    ・LOGIC: 計算、通信、データ、ビジネスロジック
    ・UI: 見た目、操作感、ユーザー接点、ドキュメント
    ・OPS: 設定、インフラ、コスト、環境、ビルド
    ・SEC: セキュリティ、機密情報、脆弱性
    ・QUAL: 品質、テスト、リファクタリング、可読性