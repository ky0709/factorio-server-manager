# Factorio Server Manager (Serverless Edition)

Discordのスラッシュコマンドから、**Amazon S3をファイルシステムとして直接マウント（S3 Files）し、セーブデータの永続化を行う**構成のFactorio専用サーバー（EC2）を、安全に操作・管理するためのサーバーレスアプリケーションです。

## 🎮 日常の運用方法
Discordのスラッシュコマンドを使用してサーバーを管理します。

| コマンド | 概要 |
| :--- | :--- |
| `/start` | サーバー（EC2）の起動・パスワード自動生成・起動確認 |
| `/stop` | 安全な停止シーケンス（セーブ・logs退避・マウント解除・EC2停止） |
| `/status` | IPアドレス、プレイヤー数、セーブ同期状態の確認 |
| `/restore` | セーブ履歴の表示 (`list`) およびデータの復元 (`select`) |
| `/save` | 現在のゲーム状態を即時セーブ |
| `/pass` | 参加用パスワードの表示 |
| `/license` | ライセンス情報の表示 |

> [!IMPORTANT]
> 各コマンドの引数や詳細な挙動、権限設定については docs/command_reference.md を参照してください。

今後の予定については [docs/roadmap.md](docs/roadmap.md) を参照してください。

## 🛠 特徴
- **サーバーレスな管理レイヤー**: Discordからのリクエストを AWS Lambda + API Gateway で直接処理するため、Factorioゲームサーバーの**管理に常駐サーバーは不要**です。Factorioゲームサーバー自体はEC2インスタンス上で動作します。
- **4層分離アーキテクチャ**: 責務を「受付（Interactor）」「実行（Executor）」「履歴・監視（Worker）」「通知（Notifier）」に分離。スケーラビリティと保守性を高め、Discordの応答制限（3秒ルール）を完全に回避します。
- **高信頼なライフサイクル管理と安全な停止シーケンス**: 無人検知や定時停止によるコスト削減に加え、Lambdaが主導する「安全な停止シーケンス」を実装。RCONセーブ、S3 への logs 退避（`logs/date=YYYY-MM-DD/` に `factorio-...__session-<UTC>.log` と `session-<UTC>.json` を保存）、OSレベルのハングアップを防ぐ **Lazy Unmount**、そしてEC2停止を適切な順序で自動制御し、データ破損リスクの低減を図っています（最終的な同期確認は `/status` や `/restore list` での反映確認を推奨）。
- **高度なセキュリティと権限管理**: Discord署名検証（Ed25519）に加え、管理者ID/ロールによるコマンド実行制限を実装。さらに、**サーバー起動ごとのランダムパスワード自動生成**機能を搭載し、セキュリティを大幅に強化。
- **ユーザビリティの向上**: IP アドレス、パスワード、バージョン ID は、タップ/クリックで簡単にコピーできるようコードブロック形式で出力されます。
- **設定値の自動同期**: ローカルの `.env` に記載した設定値を、コマンド登録時に AWS SSM へ自動同期します（機密値は `SecureString`、非機密値は `String` で保存）。
- **簡易なセーブデータ管理**: `/save` コマンドによる手動セーブ、`/restore` コマンドによるS3バージョニングを活用した過去データへの復元が可能です。
- **S3 を活用した高信頼・低コストストレージ**: 
  セーブデータの保存先に S3 を採用。EFS 等と比較して圧倒的な低コストを実現しつつ、S3 バージョニングによる多世代バックアップと、独自の同期検知ロジックによる高いデータ整合性を両立しています。詳細は docs/ec2_setup_reference.md を参照。
- **柔軟なパスワード管理**: 固定パスワードの継続利用、またはサーバー起動ごとのランダムパスワード自動生成（および RCON 経由の自動適用）を、設定（`.env`）により柔軟に選択可能です。
- **データ整合性の可視化**: S3への反映待ち状態（同期中）をリアルタイムで検知。`/status` や `/restore list` にて「同期中」ステータスを表示することで、ユーザーがデータの安全性を一目で確認できる環境を提供します。
- **自動テスト環境とインテリジェントな通知制御**: `test_runner.py` により、各LambdaのロジックやAWSリソースとの疎通を網羅的に検証可能。`--silent` モードを利用することで、DynamoDB上のセッションフラグ（TTL付き）を介して**テスト中の全通知（EventBridge経由のログ含む）を一時的に抑制**し、チャットを汚さずにコンソール上で詳細な動作確認が行えます。

## 🏗 システム構成
1. **Discord User**: `/start` / `/stop` / `/status` コマンドを実行
2. **API Gateway**: Discordからのリクエストを Interactor Lambda へ転送
3. **Interactor Lambda (Parent)**: 署名検証を行い、即座に「受付完了」を返信。Executor Lambda を非同期で起動
4. **Executor Lambda (Execution)**: EC2の起動/停止、RCONセーブ、SSMコマンド等の実効処理を担当。
5. **Worker Lambda (Management)**: 
    - **データ管理**: S3バージョニング履歴の取得、セーブデータの復元を実行。
    - **定期監視**: EventBridgeトリガーを受け、無人停止チェック (`auto-check`) を実行。
6. **Notifier Lambda (Notification)**: 
    - **対話通知**: Interaction Token を使用し、コマンドへの追加報告（Followup POST）を送信
    - **ログ通知**: インフラの状態変更やテストレポートを Webhook 経由で管理チャンネルへ投稿。
7. **DynamoDB / SSM / S3**: 状態保持（マーカー）、機密情報管理、およびセーブデータストレージ

## 🚀 技術スタック
- **Language**: Python 3.12
- **Serverless Infrastructure (管理レイヤー)**: AWS (Lambda, API Gateway, IAM, DynamoDB, SSM, EventBridge)
- **Managed Target (管理対象)**: AWS EC2 (Factorio サーバー)
- **Library**:
  - `boto3` (AWS SDK)
  - `PyNaCl` (Signature Verification)
  - `requests`, `python-dotenv`

## 📋 前提条件
- **AWS アカウント**: 管理者権限（またはリソース作成権限）を持つユーザー。
- **Discord Developer Portal**: ボットが作成済みで、`PUBLIC_KEY` と `TOKEN` が取得できていること。
- **Python 3.12**: ローカルマシンにインストール済みであること。
- **Factorio サーバー**: EC2(Linux) 上で稼働しており、RCONが有効化されていること。
- **AWS CLI v2**: ローカルマシンにインストール済みであること (インストール手順)。
- **Amazon S3 Files (s3files-utils)**: EC2 に mountpoint-s3 および s3files-utils がインストールされており、s3files タイプでマウント可能であること。
- **git**: バージョン管理および機密情報スキャンに使用。
- **EC2 IAM ロール**: `aws/IAM/FactorioServerPolicy/policy.json` に基づく S3 バケットへのアクセス権限が付与されていること。

## 📁 フォルダ構成
- `aws/`: AWS関連の設定ファイル
  - `IAM/`: 最小権限の原則（Least Privilege）に基づくポリシー設定（テンプレート `.example` と生成後の `.json`）
  - `Lambda/`: `Interactor`（受付）、`Executor`（実行）、`Worker`（履歴・監視）、`Notifier`（通知）のソースコード
- `docs/`: 開発ロードマップ等
- `scripts/`: 管理・設定用スクリプト
  - `deploy_all.py`: 全リソース（ポリシー、Layer、Lambda）の一括デプロイおよび設定同期
  - `deploy_lambda.py`: Lambda関数とLayerの個別デプロイ
  - `deploy_policies.py`: IAMポリシーのAWSへの適用
  - `init_aws_resources.py`: AWSリソース（S3, DynamoDB, IAM, Lambda）の「器」を一括作成
  - `cleanup_aws_resources.sh`: 作成したAWSリソースを完全に削除
  - `setup_config.py`: `.env` の値を使用して IAM ポリシーのテンプレートを生成
  - `register.py`: Discordコマンドの登録および機密情報（SSM）の同期
  - `check_env_leaks.py`: Git履歴内の機密情報漏洩スキャン（関数名等の偽陽性を除外するフィルタリング機能付き）
  - `test_runner.py`: 各種Lambda関数やAWSリソースの自動テスト実行
  - `update_eventbridge.py`: EventBridgeスケジュールの設定・更新
  - `update_layer.py`: 共通処理用のLambda Layerの更新
- `.env`: ローカル環境用の認証情報およびAWS同期用設定（Git管理対象外）
- `requirements.txt`: ローカル環境用ライブラリ

## 🏷 リソースの命名規則
本プロジェクトでは、最小権限の原則に基づき、管理用ポリシー（`FactorioRegistPolicy`）で操作可能なリソースを名前の前方一致で制限しています。`.env` で独自の名前を設定する場合は、以下の接頭辞を維持してください。

- **S3 バケット**: `factorio-` で始まる必要があります（例: `factorio-storage-xxx`）。
- **DynamoDB / Lambda / IAM**: 原則 `Factorio` で始まる必要があります（例: `FactorioState`, `Factorio_Executor`）。  
  例外として EC2 管理対象ロール/プロファイルは `EC2-Factorio-*` 命名にも対応しています。
- **EventBridge / Scheduler**: 自動的に `Factorio-` 接頭辞が付与されます。

## ✅ 開発者向けセットアップ・チェックリスト
新規に環境を構築する際は、以下の項目を順に完了させてください。

### 1. Discord 側の準備
- [ ] Discord Developer Portal で Application を作成する
- [ ] Bot を作成し `DISCORD_TOKEN` を取得する
- [ ] `APP_ID` (Application ID) と `DISCORD_PUBLIC_KEY` を取得する
- [ ] 導入先サーバーの `GUILD_ID` を取得する
- [ ] 通知用とログ用の Webhook を作成し、それぞれの URL を取得する
- [ ] 管理者権限を付与するユーザー ID (`ADMIN_USER_IDS`) またはロール ID (`ADMIN_ROLE_IDS`) を特定する

### 2. AWS 側の準備
- [ ] 使用する `AWS_REGION` を決定する (例: `ap-northeast-1`)
- [ ] `AWS_ACCOUNT_ID` を確認する
- [ ] 管理対象となる EC2 インスタンスを起動し、`INSTANCE_ID` を取得する（既存サーバーを流用する場合も同様）
- [ ] **[決定]** 作成する S3 バケット名、DynamoDB テーブル名、Lambda 関数名、IAM ポリシー名を決める

### 3. ローカル環境の構築
- [ ] `python --version` が 3.12 以上であることを確認する
- [ ] `.env.example` をコピーして `.env` (本番) または `.env.dev` (開発) を作成する
- [ ] 上記で収集・決定した値を `.env` に記入する（`S3_FILES_SYSTEM_ID` は `init_aws_resources` 後に `aws s3files list-file-systems` で確認し、`.env` / `.env.dev` に追記する。詳細は下記「### 2. AWS：インフラリソースの構築」内の手順参照）
- [ ] `pip install -r requirements.txt` を実行して依存関係をインストールする
- [ ] `aws configure --profile <name>` で、適切な権限を持つプロファイルを作成する

### 4. 初期デプロイフロー
- [ ] `python scripts/check_env_leaks.py <env>` を実行し、機密情報の漏洩がないか確認する
- [ ] `python scripts/init_aws_resources.py <env>` を実行して、AWS上にベースリソースを作成する
- [ ] `python scripts/setup_config.py` を実行して、環境に合わせた IAM ポリシーファイルを生成する
- [ ] Interactor Lambda に PyNaCl を含む Layer をアタッチする（デプロイ後に Layer 設定を確認）
- [ ] `python scripts/deploy_all.py <env> --silent` を実行して、全コンポーネントをデプロイし、通知を飛ばさずにテストを完走させる
- [ ] `python scripts/test_runner.py` を実行して、システム全体の疎通を確認する

---

## 📝 詳細なセットアップ手順

### 1. ローカル：機密情報の準備と同期
1. **[ローカル]** `.env.example` をコピーして `.env` または `.env.dev` を作成し、必要な設定値を入力する。
2. **[ローカル]** `pip install -r requirements.txt` を実行して依存ライブラリをインストールする。
3. **[ローカル]** `aws configure --profile <profile_name>` を実行（開発用なら `factorio-dev`、本番用なら `factorio-prod` 等）し、適切な権限を持つプロファイルを作成する。
4. **[ローカル]** `.env` / `.env.dev` の `SERVICE_UNIT_NAME` を対象環境の systemd ユニット名へ設定する（例: `factorio`, `factorio-dev`, `factorio-prod`）。
   - 本プロジェクトの標準構成（Factorio専用EC2）ではリスクは低いが、独自構築サーバーで他プロセス/他ユニットを同居させる場合は、誤ったユニット名を指定すると意図しないサービス停止リスクがあるため、適用前に `systemctl status <SERVICE_UNIT_NAME>` で対象を確認すること。

### 2. AWS：インフラリソースの構築
1. **[ローカル]** `python scripts/init_aws_resources.py <env>` を実行し、S3, DynamoDB, IAM Role, EventBridge, Lambda の器を自動作成する。
   - `S3_FILES_SYSTEM_ID` が未設定の場合、S3 Files 用 IAM ロール（`S3_FILES_SERVICE_ROLE_NAME`、既定 `FactorioS3FilesServiceRole`）を用意したうえで、AWS CLI (`s3files create-file-system`) によりファイルシステム作成を試行します。初回は `setup_config.py` と `deploy_policies.py` で Regist ポリシー更新後に再実行してください。
   - **ファイルシステム ID の確認**: 作成の成否にかかわらず、次で一覧し、`.env` の `S3_BUCKET_NAME` に対応する行の `fileSystemId`（`fs-...`）を控える（複数環境がある場合はバケット ARN で見分ける）。

```powershell
aws s3files list-file-systems --profile <profile_name> --region <REGION> --output table
```

   - **`.env` への反映**: 控えた `fs-...` を `.env` または `.env.dev` の `S3_FILES_SYSTEM_ID` に設定する（EC2 の `/etc/fstab` のマウント元と同じ値にする）。
   - 既存EC2があり `INSTANCE_ID` が `.env` に設定されている場合、EC2用ロール/インスタンスプロファイル（例: `EC2-Factorio-Server-Role-dev`）の作成・アタッチまで実施されます。
2. **[ローカル]** `python scripts/setup_config.py <env>` を実行して、環境に合わせた実際の IAM ポリシーファイルをローカルに生成する。
3. **[ローカル]** `python scripts/deploy_policies.py <env>` を実行して、生成したポリシーを AWS へ適用する。
4. **[Lambda]** Interactor 関数に PyNaCl を含む Layer が設定されていることを確認する（未設定ならコンソールまたは CLI で Layer を追加）。
5. **[API Gateway]** Discord Interactions 受け口（`POST /interactions`）を手動で作成し、`Factorio_Interactor-<env>` に接続する。

#### API Gateway 手動作成（初回のみ）

`apigatewayv2` の `create-api` は `FactorioRegistUser-dev` などの管理ユーザーに `apigateway:POST` 権限が必要です。  
権限不足の場合は、上位権限ユーザーで一度だけ作成してください。

PowerShell で ARN 文字列を組み立てるときは `"$REGION:$ACCOUNT_ID"` のような表記で構文エラーになるため、`${REGION}` の形式を使用してください。

```powershell
# 例: dev
$AWS_PROFILE = "factorio-dev"
$REGION = "ap-northeast-1"
$ACCOUNT_ID = "<ACCOUNT_ID>"
$LAMBDA_NAME = "Factorio_Interactor-dev"
$API_NAME = "FactorioControlAPI-dev"

# 1) HTTP API 作成
$API_ID = aws apigatewayv2 create-api `
  --name $API_NAME `
  --protocol-type HTTP `
  --profile $AWS_PROFILE `
  --region $REGION `
  --query "ApiId" `
  --output text

# 2) Lambda統合
$LAMBDA_ARN = "arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${LAMBDA_NAME}"
$INTEGRATION_ID = aws apigatewayv2 create-integration `
  --api-id $API_ID `
  --integration-type AWS_PROXY `
  --integration-uri $LAMBDA_ARN `
  --payload-format-version "2.0" `
  --profile $AWS_PROFILE `
  --region $REGION `
  --query "IntegrationId" `
  --output text

# 3) ルート作成
aws apigatewayv2 create-route `
  --api-id $API_ID `
  --route-key "POST /interactions" `
  --target "integrations/$INTEGRATION_ID" `
  --profile $AWS_PROFILE `
  --region $REGION

# 4) ステージ作成
aws apigatewayv2 create-stage `
  --api-id $API_ID `
  --stage-name '$default' `
  --auto-deploy `
  --profile $AWS_PROFILE `
  --region $REGION

# 5) API Gateway -> Lambda invoke 許可
$SOURCE_ARN = "arn:aws:execute-api:${REGION}:${ACCOUNT_ID}:${API_ID}/*/POST/interactions"
aws lambda add-permission `
  --function-name $LAMBDA_NAME `
  --statement-id "AllowInvokeFromApiGateway-$API_ID" `
  --action "lambda:InvokeFunction" `
  --principal "apigateway.amazonaws.com" `
  --source-arn $SOURCE_ARN `
  --profile $AWS_PROFILE `
  --region $REGION

# 6) Discord に設定する URL
$API_ENDPOINT = aws apigatewayv2 get-api `
  --api-id $API_ID `
  --profile $AWS_PROFILE `
  --region $REGION `
  --query "ApiEndpoint" `
  --output text

Write-Host "$API_ENDPOINT/interactions"
```

この URL を Discord Developer Portal の dev アプリ `Interactions Endpoint URL` に設定し、最後に `python scripts/register.py dev` を実行して反映してください。

### 3. サーバー：EC2 側及びデータの初期化
1. **[インフラ]** EC2 インスタンスのセキュリティグループにて、Lambda からの RCON 通信（`RCON_PORT` で設定した TCP ポート）を許可する。
2. **[サーバー]** Factorio サーバーの RCON を有効化する。
3. **[IAM]** EC2 インスタンスに S3 アクセス権限を持つロールを適用する。
4. **[サーバー]** S3 Files を使用してセーブデータディレクトリをマウントし、`_netdev` オプションを適切に設定する。

> [!TIP]
> EC2 および S3 Files の具体的なセットアップ手順については docs/ec2_setup_reference.md を参照してください。

## 🚀 デプロイ
以下のコマンドで、全てのコード、ポリシー、設定、Discordコマンドを最新の状態に更新できます。

```powershell
# 全リソースの更新とテスト実行（チャット通知あり）
python scripts/deploy_all.py <env>

# チャット通知を抑制してデプロイとテストを実行
python scripts/deploy_all.py <env> --silent
```

## ⚙️ 設定の変更方法
無人停止時間や権限設定などを変更したい場合は、以下の手順で行います。
1. **`.env` の修正**: ローカルの `.env` ファイル内の該当する変数を書き換えます。
2. **再デプロイ**: `python scripts/deploy_all.py <env>` を実行します。
   - これにより、Lambda の環境変数、EventBridge のスケジュール、SSM パラメータ、IAM ポリシーが最新の状態に更新されます。
3. **反映の確認**: `/status` コマンドや実際の挙動で変更が適用されたことを確認します。

## ⚠️ 運用上の注意
- **メンテナンス時の自動停止**: 
  サーバーのアップデートや設定変更などのメンテナンス作業を行う際は、必ず **EventBridge Scheduler のトリガー（無人監視・定時停止）を「無効 (Disable)」** にしてください。
  作業中にプレイヤーが 0 人の状態が続くと、自動停止ロジックが作動してインスタンスが強制的にシャットダウンされる可能性があります。

## ⚖️ License
[MIT License](LICENSE)