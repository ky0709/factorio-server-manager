プロジェクト管理表
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
    ・背景: 固定インスタンスの起動停止運用を維持しつつ、起動テンプレート経由の新規作成（spot/ondemand）と終了を切り替え可能にし、運用コストと可用性の選択肢を持たせる必要があるため。
    ・完了条件: SERVER_RUN_MODEおよびINSTANCE_PROVISIONING_TYPEを使った分岐方針、起動テンプレート利用時のrun/terminateフロー、アクティブInstanceIdの保持先（DynamoDB等）、Spot中断時の退避方針、必要IAM権限の差分、実装着手手順（ブランチ作成開始を含む）がコード内TODOとtasks.mdから追跡可能になっていること。

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

[ ] ID:027 [TASK] [LOGIC] save/stop の S3 反映確認と巻き戻しを Lambda 側完結へ移行
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

・運用ルール: 未完了は[ ]、完了後は[x]に更新する

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