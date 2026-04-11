# Factorio Server Manager (Serverless Edition)

Discordのスラッシュコマンドから、AWS上のFactorio専用サーバー（EC2）を起動・停止・状態確認するためのサーバーレスアプリケーションです。

今後の予定については [docs/roadmap.md](docs/roadmap.md) を参照してください。

## 🛠 特徴
- **サーバーレスな管理レイヤー**: Discordからのリクエストを AWS Lambda + API Gateway で直接処理するため、Factorioゲームサーバーの**管理に常駐サーバーは不要**です。Factorioゲームサーバー自体はEC2インスタンス上で動作します。
- **4層分離アーキテクチャ**: 責務を「受付（Interactor）」「実行（Executor）」「履歴・監視（Worker）」「通知（Notifier）」に分離。スケーラビリティと保守性を高め、Discordの応答制限（3秒ルール）を完全に回避します。
- **多重系の停止ロジック (Robust Shutdown)**: 無人検知と定時停止を組み合わせた堅牢なコスト最適化に加え、OSレベルの `_netdev` 制御と Lambda からの **Lazy Unmount** 命令による二段構えの保護を実装。ネットワーク切断時の OS フリーズや NFS ハングアップを徹底的に排除したクリーンシャットダウンを実現しています。
- **高度なセキュリティと権限管理**: Discord署名検証（Ed25519）に加え、SSM管理された管理者ID/ロールによるコマンド実行制限（デフォルト：`/restore select`）を実装。制限対象は `.env` の `RESTRICTED_COMMAND_STRINGS` で自由に変更可能で、設定されたコマンドには Discord 上の説明文に自動的に「[管理者限定]」のタグが付与されます。
- **機密情報の自動同期**: ローカルの `.env` に記載した機密情報を、コマンド登録時に AWS SSM へ自動的に同期・アップロードします。
- **簡易なセーブデータ管理**: `/save` コマンドによる手動セーブ、`/restore` コマンドによるS3バージョニングを活用した過去データへの復元が可能です。
- **Amazon S3 Files (s3files-utils) によるコスト効率と機能性**: 
  従来の EFS や Mountpoint for Amazon S3 の制限を打破し、s3files-utils を用いることで以下の機能を実現しています：
  - **圧倒的なコスト削減**: EFS と比較してストレージおよびスループットコストを大幅に抑制。
  - **完全なファイルシステム互換性**: 従来の S3 マウント（例: s3fs-fuse）では不可能だった「ファイルロック」「POSIX 権限」「共有書き込みアクセス」「完全なメタデータ操作」をサポート。
  - **断続的アクセスの最適化**: Factorio のセーブデータのような「書き込み時のみ高負荷」なパターンに特化したコスト構造。
  - **既存アプリとの完全互換**: ゲームサーバー本体に一切の変更を加えず、標準のローカルディレクトリとして透過的に利用可能。

- **データ整合性と可視化**: 停止時に `Factorio停止` -> `カタログ更新` -> `アンマウント` -> `EC2停止` を自動実行。S3への反映待ち状態（同期中）をリアルタイムで検知し、`/status` や `/restore list` に表示することで、データ喪失を防ぎます。
- **自動テスト環境**: `test_runner.py` により、各LambdaのロジックやAWSリソースとの疎通を網羅的に検証可能。テスト結果はDiscordのログチャットへ自動レポートされます。

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
- **Infrastructure**: AWS (Lambda, API Gateway, EC2, IAM, DynamoDB, SSM, EventBridge)
- **Library**:
  - `boto3` (AWS SDK)
  - `PyNaCl` (Signature Verification)
  - `requests`, `python-dotenv`

## 📋 前提条件
- **AWS アカウント**: 管理者権限（またはリソース作成権限）を持つユーザー。
- **Discord Developer Portal**: ボットが作成済みで、`PUBLIC_KEY` と `TOKEN` が取得できていること。
- **Python 3.12**: ローカルマシンにインストール済みであること。
- **Factorio サーバー**: EC2(Linux) 上で稼働しており、RCONが有効化されていること。
- **Amazon S3 Files (s3files-utils)**: EC2 に mountpoint-s3 および s3files-utils がインストールされており、s3files タイプでマウント可能であること。
- **EC2 IAM ロール**: `aws/IAM/FactorioServerPolicy/policy.json` に基づく S3 バケットへのアクセス権限が付与されていること。

##  フォルダ構成
- `aws/`: AWS関連の設定ファイル
  - `IAM/`: 最小権限の原則（Least Privilege）に基づくポリシー設定（テンプレート `.example` と生成後の `.json`）
  - `Lambda/`: `Interactor`（受付）、`Executor`（実行）、`Notifier`（通知）のソースコード
- `docs/`: 開発ロードマップ等
- `scripts/`: 管理・設定用スクリプト
  - `setup_config.py`: `.env` の値を使用して IAM ポリシーのテンプレートを置換・生成するスクリプト
  - `register.py`: Discordコマンドの登録および機密情報（SSM）の同期を行うユーティリティ
- `.env`: ローカル環境用の認証情報およびAWS同期用設定（Git管理対象外）
- `requirements.txt`: ローカル環境用ライブラリ

## 🆕 開発者向けセットアップ・チェックリスト
新規に環境を構築する際は、以下の項目を順に完了させてください。

### 1. Discord 側の準備
- [ ] Discord Developer Portal で Application を作成する
- [ ] Bot を作成し `DISCORD_TOKEN` を取得する
- [ ] `APP_ID` (Application ID) と `DISCORD_PUBLIC_KEY` を取得する
- [ ] 導入先サーバーの `GUILD_ID` を取得する
- [ ] 通知用とログ用の Webhook を作成し、それぞれの URL を取得する
- [ ] 管理者として権限を付与するユーザーの ID (`ADMIN_USER_IDS`) を特定する

### 2. AWS 側の準備
- [ ] 使用する `AWS_REGION` を決定する (例: `ap-northeast-1`)
- [ ] `AWS_ACCOUNT_ID` を確認する
- [ ] Factorio 用の EC2 インスタンスを起動し、`INSTANCE_ID` を取得する
- [ ] セーブデータ保存用の S3 バケットを作成する
- [ ] 状態管理用の DynamoDB テーブル (`DYNAMODB_TABLE_NAME`) を作成する (パーティションキー: `ConfigKey`)
- [ ] 使用する Lambda 関数名および IAM ポリシー名がプロジェクトの命名規則に合っているか確認する

### 3. ローカル環境の構築
- [ ] `python --version` が 3.12 以上であることを確認する
- [ ] `.env.example` をコピーして `.env` を作成する
- [ ] 上記 1, 2 で収集した値をすべて `.env` に記入する
- [ ] `pip install -r requirements.txt` を実行して依存関係をインストールする
- [ ] `aws configure` 等で、デプロイに必要な適切な権限を持つ AWS 認証情報を設定する

### 4. 初期デプロイフロー
- [ ] `python scripts/setup_config.py` を実行して、環境に合わせた IAM ポリシーファイルを生成する
- [ ] `python scripts/deploy_policies.py` を実行して、AWS 上に IAM ポリシーをデプロイする
- [ ] `python scripts/update_layer.py` を実行して、共通モジュール (Lambda Layer) をデプロイする
- [ ] `python scripts/deploy_lambda.py` を実行して、Lambda 関数をデプロイする
- [ ] `python scripts/register.py` を実行して、Discord コマンドの登録と SSM への機密情報同期を行う
- [ ] `python scripts/test_runner.py` を実行して、システム全体の疎通を確認する

---

## 📝 詳細なセットアップ手順

### 1. ローカル：機密情報の準備と同期
1. **[ローカル]** `.env.example` をコピーして `.env` を作成し、Discord の各設定値と、AWS の `AWS_REGION`, `AWS_ACCOUNT_ID`, `INSTANCE_ID`, `S3_BUCKET_NAME` を入力する。**`AWS_ACCESS_KEY_ID` と `AWS_SECRET_ACCESS_KEY` は、ステップ4でIAMユーザーのアクセスキーが発行された後に追記してください。**
2. **[ローカル]** `pip install -r requirements.txt` を実行して依存ライブラリをインストールする。
3. **[ローカル]** `python scripts/setup_config.py` を実行し、プレースホルダーを置換した `policy.json` を一括生成する。
4. **[AWSコンソール]** IAM ユーザー `FactorioRegistUser` を作成し、生成された `aws/IAM/FactorioRegistPolicy/policy.json` の権限を適用してアクセスキーを発行する。
5. **[ローカル]** `python scripts/register.py` を実行し、Discord コマンドの登録と SSM Parameter Store への機密情報同期を行う。

### 2. AWS：インフラリソースの構築
1. **[AWSコンソール]** DynamoDB テーブル `FactorioState` を作成する（パーティションキー: `ConfigKey` (文字列)）。
2. **[AWSコンソール]** Lambda 関数 (Interactor, Executor, Notifier) の環境変数を以下の通り設定する。
    - ※機密情報やリソース名は `register.py` によって SSM Parameter Store へ同期されるため、手動設定は最小限で済みます。
    - 署名検証用の `PyNaCl` レイヤーを `Factorio_Interactor` に適用してください。
    - 共通ユーティリティレイヤーは `scripts/update_layer.py` によって自動的に紐付けられます。
7. **[AWSコンソール]** EventBridge Scheduler を作成し、それぞれ以下のターゲットと JSON ペイロードで呼び出すように設定する。
    - **無人監視 (5分おき)**: ターゲット `Factorio_Worker` / ペイロード `{"action": "auto-check"}`
    - **定時停止 (毎日深夜など)**: ターゲット `Factorio_Executor` / ペイロード `{"action": "stop"}`
    - **EC2状態通知**: EventBridgeルールを作成し、`stopped`/`running` 時に `Factorio_Executor` を呼び出すよう設定します。

### 3. サーバー：EC2 側及びデータの初期化
1. **[AWSコンソール]** セキュリティグループで、Lambda/VPC からの **TCP 27015** (RCON) および **UDP 34197** (Factorio) を許可する。
2. **[SSH]** Factorio サーバーの起動オプションに `--rcon-port 27015 --rcon-password <パスワード>` を追加する。
3. **[AWSコンソール]** EC2 インスタンスに付与する IAM ロールを作成し、`aws/IAM/FactorioServerPolicy/policy.json` の権限を適用する。
4. **[SSH]** S3 Files マウントポイントの作成と権限設定:
   ```bash
   sudo mkdir -p /mnt/factorio-saves
   sudo chown factorio:factorio /mnt/factorio-saves
   ```
5. **[SSH]** `/etc/fstab` に以下の行を追記し、恒久的なマウント設定を行う。
   `<S3_FILES_SYSTEM_ID>:/  /mnt/factorio-saves  s3files  _netdev,rw,allow_other  0  0`
   - `<S3_FILES_SYSTEM_ID>:/` は、S3バケットを識別するためのIDです。実際の環境に合わせて置き換えてください。
   - `s3files` は Mountpoint for Amazon S3 (s3files-utils) のマウントタイプです。
   - `_netdev` オプションは、ネットワークの準備を待ってマウント/アンマウントするために不可欠です。
   - `allow_other` オプションは、`factorio` 実行ユーザーがマウントポイントにアクセスするために必須です。
   - **注意**: `/etc/fstab` 設定後は `sudo systemctl daemon-reload` を実行し、`sudo mount -a` でフリーズしないことを確認してください。
6. **[AWSコンソール]** DynamoDB の `FactorioState` テーブルに、初期データとして `{ "ConfigKey": "ZeroPlayerCount", "CountValue": 0 }` を手動で作成する。

## 🚀 デプロイ
1. **[AWSコンソール/CLI]** `aws/Lambda/Factorio_Interactor/lambda_function.py` をデプロイ。
2. **[AWSコンソール/CLI]** `aws/Lambda/Factorio_Executor/lambda_function.py` をデプロイ。
3. **[AWSコンソール/CLI]** `aws/Lambda/Factorio_Notifier/lambda_function.py` をデプロイ。
4. **[AWSコンソール]** 各Lambdaの実行ロールに適切なポリシーを適用する。
    - `Factorio_Interactor`: `aws/IAM/FactorioInteractPolicy/policy.json`
    - `Factorio_Executor`: `aws/IAM/FactorioExecutePolicy/policy.json`
    - `Factorio_Notifier`: `aws/IAM/FactorioNotifyPolicy/policy.json`

## ⚠️ 運用上の注意
- **メンテナンス時の自動停止**: 
  サーバーのアップデートや設定変更などのメンテナンス作業を行う際は、必ず **EventBridge Scheduler のトリガー（無人監視・定時停止）を「無効 (Disable)」** にしてください。
  作業中にプレイヤーが 0 人の状態が続くと、自動停止ロジックが作動してインスタンスが強制的にシャットダウンされる可能性があります。

## ⚖️ License
[MIT License](LICENSE)
