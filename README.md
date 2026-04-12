# Factorio Server Manager (Serverless Edition)

Discordのスラッシュコマンドから、**Amazon S3をファイルシステムとして直接マウント（S3 Files）し、セーブデータの永続化を行う**構成のFactorio専用サーバー（EC2）を、安全に操作・管理するためのサーバーレスアプリケーションです。

## 🎮 日常の運用方法
Discordのスラッシュコマンドを使用してサーバーを管理します。

| コマンド | 概要 | 権限 |
| :--- | :--- | :--- |
| `/start` | サーバー（EC2）の起動と起動確認 | 管理者/一般 |
| `/stop` | 安全な停止シーケンス（セーブ・マウント解除・EC2停止） | 管理者/一般 |
| `/status` | IPアドレス、プレイヤー数、セーブ同期状態の確認 | 全員 |
| `/restore` | セーブ履歴の表示 (`list`) およびデータの復元 (`select`) | `select`は管理者のみ |
| `/save` | 現在のゲーム状態を即時セーブ | 管理者/一般 |
| `/pass` | 参加用パスワードの表示 | 全員 |

> [!IMPORTANT]
> 各コマンドの引数や詳細な挙動、権限設定については docs/command_reference.md を参照してください。

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
- **AWS CLI v2**: ローカルマシンにインストール済みであること (インストール手順)。
- **Amazon S3 Files (s3files-utils)**: EC2 に mountpoint-s3 および s3files-utils がインストールされており、s3files タイプでマウント可能であること。
- **git**: バージョン管理および機密情報スキャンに使用。
- **EC2 IAM ロール**: `aws/IAM/FactorioServerPolicy/policy.json` に基づく S3 バケットへのアクセス権限が付与されていること。

##  フォルダ構成
- `aws/`: AWS関連の設定ファイル
  - `IAM/`: 最小権限の原則（Least Privilege）に基づくポリシー設定（テンプレート `.example` と生成後の `.json`）
  - `Lambda/`: `Interactor`（受付）、`Executor`（実行）、`Notifier`（通知）のソースコード
- `docs/`: 開発ロードマップ等
- `scripts/`: 管理・設定用スクリプト
  - `init_aws_resources.sh`: AWSリソース（S3, DynamoDB, IAM, Lambda）の「器」を一括作成
  - `cleanup_aws_resources.sh`: 作成したAWSリソースを完全に削除
  - `setup_config.py`: `.env` の値を使用して IAM ポリシーのテンプレートを生成
  - `register.py`: Discordコマンドの登録および機密情報（SSM）の同期
  - `check_env_leaks.py`: Git履歴内の機密情報漏洩をスキャン
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
- [ ] 管理者権限を付与するユーザー ID (`ADMIN_USER_IDS`) またはロール ID (`ADMIN_ROLE_IDS`) を特定する

### 2. AWS 側の準備
- [ ] 使用する `AWS_REGION` を決定する (例: `ap-northeast-1`)
- [ ] `AWS_ACCOUNT_ID` を確認する
- [ ] 管理対象となる EC2 インスタンスを起動し、`INSTANCE_ID` を取得する
- [ ] **[決定]** 作成する S3 バケット名、DynamoDB テーブル名、Lambda 関数名、IAM ポリシー名を決める

### 3. ローカル環境の構築
- [ ] `python --version` が 3.12 以上であることを確認する
- [ ] `.env.example` をコピーして `.env` (本番) または `.env.dev` (開発) を作成する
- [ ] 上記で収集・決定した値を `.env` に記入する (`S3_FILES_SYSTEM_ID` は後ほど EC2 設定時に追記で可)
- [ ] `pip install -r requirements.txt` を実行して依存関係をインストールする
- [ ] `aws configure --profile <name>` で、適切な権限を持つプロファイルを作成する

### 4. 初期デプロイフロー
- [ ] `python scripts/check_env_leaks.py <env>` を実行し、機密情報の漏洩がないか確認する
- [ ] `scripts/init_aws_resources.sh <env>` を実行して、AWS上にリソースの器を作成する
- [ ] `python scripts/setup_config.py` を実行して、環境に合わせた IAM ポリシーファイルを生成する
- [ ] `python scripts/deploy_policies.py` を実行して、AWS 上に IAM ポリシーをデプロイする
- [ ] `python scripts/update_layer.py` を実行して、共通モジュール (Lambda Layer) をデプロイする
- [ ] `python scripts/deploy_lambda.py` を実行して、Lambda 関数をデプロイする
- [ ] `python scripts/register.py` を実行して、Discord コマンドの登録と SSM への機密情報同期を行う
- [ ] `python scripts/test_runner.py` を実行して、システム全体の疎通を確認する

---

## 📝 詳細なセットアップ手順

### 1. ローカル：機密情報の準備と同期
1. **[ローカル]** `.env.example` をコピーして `.env` または `.env.dev` を作成し、必要な設定値を入力する。
2. **[ローカル]** `pip install -r requirements.txt` を実行して依存ライブラリをインストールする。
4. **[ローカル]** `aws configure --profile <profile_name>` を実行（開発用なら `factorio-dev`、本番用なら `factorio-prod` 等）し、適切な権限を持つプロファイルを作成する。
5. **[ローカル]** `python scripts/check_env_leaks.py <env>` を実行し、Git履歴に機密情報が含まれていないか確認する。

### 2. AWS：インフラリソースの構築
1. **[ローカル]** `scripts/init_aws_resources.sh <env>` を実行し、S3, DynamoDB, IAM Role, EventBridge, Lambda の器を自動作成する。
2. **[ローカル]** `python scripts/setup_config.py <env>` を実行して、環境に合わせた実際の IAM ポリシーファイルをローカルに生成する。
3. **[ローカル]** `python scripts/deploy_policies.py <env>` を実行して、生成したポリシーを AWS へ適用する。

### 3. サーバー：EC2 側及びデータの初期化
1. **[インフラ]** EC2 インスタンスのセキュリティグループにて、Lambda からの RCON 通信 (TCP 27015) を許可する。
2. **[サーバー]** Factorio サーバーの RCON を有効化する。
3. **[IAM]** EC2 インスタンスに S3 アクセス権限を持つロールを適用する。
4. **[サーバー]** S3 Files を使用してセーブデータディレクトリをマウントし、`_netdev` オプションを適切に設定する。

> [!TIP]
> EC2 および S3 Files の具体的なセットアップ手順については docs/ec2_setup_reference.md を参照してください。

## 🚀 デプロイ
リソースの器が作成されたら、以下のスクリプトでコードと設定を反映させます。
1. **[ローカル]** `python scripts/update_layer.py <env>` を実行し、共通ユーティリティをデプロイ。
2. **[ローカル]** `python scripts/deploy_lambda.py <env>` を実行し、全 Lambda 関数をデプロイ。
3. **[ローカル]** `python scripts/register.py <env>` を実行し、Discord コマンド登録と機密情報 (SSM) の同期を行う。
4. **[ローカル]** `python scripts/test_runner.py <env>` を実行して、疎通を確認。

## ⚠️ 運用上の注意
- **メンテナンス時の自動停止**: 
  サーバーのアップデートや設定変更などのメンテナンス作業を行う際は、必ず **EventBridge Scheduler のトリガー（無人監視・定時停止）を「無効 (Disable)」** にしてください。
  作業中にプレイヤーが 0 人の状態が続くと、自動停止ロジックが作動してインスタンスが強制的にシャットダウンされる可能性があります。

## ⚖️ License
[MIT License](LICENSE)
