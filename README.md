# Factorio Server Manager (Serverless Edition)

Discordのスラッシュコマンドから、AWS上のFactorio専用サーバー（EC2）を起動・停止・状態確認するためのサーバーレスアプリケーションです。

今後の予定については [docs/roadmap.md](docs/roadmap.md) を参照してください。

## 🛠 特徴
- **サーバーレスな管理レイヤー**: Discordからのリクエストを AWS Lambda + API Gateway で直接処理するため、Factorioゲームサーバーの**管理に常駐サーバーは不要**です。Factorioゲームサーバー自体はEC2インスタンス上で動作します。
- **非同期アーキテクチャ**: Discordの「3秒ルール（応答制限）」を回避するため、受付用（Interactions）と実行用（Executor）の親子Lambda構成を採用しています。
- **多重系の停止ロジック (Robust Shutdown)**: 無人検知と定時停止を組み合わせた堅牢なコスト最適化に加え、OSレベルの `_netdev` 制御と Lambda からの **Lazy Unmount** 命令による二段構えの保護を実装。ネットワーク切断時の OS フリーズや NFS ハングアップを徹底的に排除したクリーンシャットダウンを実現しています。
- **セキュリティ**: Discordからのリクエスト署名検証（Ed25519）を実装し、不正なアクセスを遮断します。
- **機密情報の自動同期**: ローカルの `.env` に記載した機密情報を、コマンド登録時に AWS SSM へ自動的に同期・アップロードします。
- **簡易なセーブデータ管理**: `/save` コマンドによる手動セーブ、`/restore` コマンドによるS3バージョニングを活用した過去データへの復元が可能です。
- **Amazon S3 Files (s3files-utils) によるコスト効率と機能性**: 
  従来の EFS や Mountpoint for Amazon S3 の制限を打破し、s3files-utils を用いることで以下の機能を実現しています：
  - **圧倒的なコスト削減**: EFS と比較してストレージおよびスループットコストを大幅に抑制。
  - **完全なファイルシステム互換性**: 従来の S3 マウント（例: s3fs-fuse）では不可能だった「ファイルロック」「POSIX 権限」「共有書き込みアクセス」「完全なメタデータ操作」をサポート。
  - **断続的アクセスの最適化**: Factorio のセーブデータのような「書き込み時のみ高負荷」なパターンに特化したコスト構造。
  - **既存アプリとの完全互換**: ゲームサーバー本体に一切の変更を加えず、標準のローカルディレクトリとして透過的に利用可能。

- **データ整合性の保証**: 停止時に `Factorio停止` -> `OSキャッシュのフラッシュ(sync)` -> `SSM経由のNFSアンマウント` を自動実行。S3 への書き込み完了（ライトバック）を確実にしてからインスタンスを停止するため、データ破損と書き込み漏れのリスクを最小限に抑えます。

## 🏗 システム構成
1. **Discord User**: `/start` / `/stop` / `/status` コマンドを実行
2. **API Gateway**: Discordからのリクエストを Interactor Lambda へ転送
3. **Interactor Lambda (Parent)**: 署名検証を行い、即座に「受付完了」を返信。Executor Lambda を非同期で起動
4. **Executor Lambda (Child)**: 
    - **コマンド実行**: EC2の起動/停止、RCON経由のセーブ、および **SSM経由の S3 Files アンマウント** を実行。完了後に Discord の初期応答を PATCH で更新
    - **定期監視**: EventBridgeからのトリガーを受け、プレイヤー数をチェック。無人状態が続けば自動停止を実行
5. **DynamoDB**: 無人状態の継続回数を保持
6. **SSM Parameter Store**: RCONパスワードやDiscord Webhook URLなどの機密情報を安全に保持
7. **Amazon S3**: S3 Files (s3files-utils) を通じて EC2 にマウントされ、セーブデータを保持

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
- **EC2 IAM ロール**: `aws/IAM/EC2-Factorio-Server-RolePolicy/policy.json` に基づく S3 バケットへのアクセス権限が付与されていること。

##  フォルダ構成
- `aws/`: AWS関連の設定ファイル
  - `IAM/`: 最小権限の原則（Least Privilege）に基づくポリシー設定（テンプレート `.example` と生成後の `.json`）
  - `Lambda/`: `Interactions`（受付用）と `Executor`（実行用）のソースコード
- `docs/`: 開発ロードマップ等
- `scripts/`: 管理・設定用スクリプト
  - `setup_config.py`: `.env` の値を使用して IAM ポリシーのテンプレートを置換・生成するスクリプト
  - `register.py`: Discordコマンドの登録および機密情報（SSM）の同期を行うユーティリティ
- `.env`: ローカル環境用の認証情報およびAWS同期用設定（Git管理対象外）
- `requirements.txt`: ローカル環境用ライブラリ

## 📝 セットアップ

### 1. ローカル：機密情報の準備と同期
1. **[ローカル]** `.env.example` をコピーして `.env` を作成し、Discord の各設定値と、AWS の `AWS_REGION`, `AWS_ACCOUNT_ID`, `INSTANCE_ID`, `S3_BUCKET_NAME` を入力する。**`AWS_ACCESS_KEY_ID` と `AWS_SECRET_ACCESS_KEY` は、ステップ4でIAMユーザーのアクセスキーが発行された後に追記してください。**
2. **[ローカル]** `pip install -r requirements.txt` を実行して依存ライブラリをインストールする。
3. **[ローカル]** `python scripts/setup_config.py` を実行し、プレースホルダーを置換した `policy.json` を一括生成する。
4. **[AWSコンソール]** IAM ユーザー `FactorioRegistUser` を作成し、生成された `aws/IAM/FactorioRegistPolicy/policy.json` の権限を適用してアクセスキーを発行する。
5. **[ローカル]** `python scripts/register.py` を実行し、Discord コマンドの登録と SSM Parameter Store への機密情報同期を行う。

### 2. AWS：インフラリソースの構築
1. **[AWSコンソール]** DynamoDB テーブル `FactorioState` を作成する（パーティションキー: `ConfigKey` (文字列)）。
2. **[AWSコンソール]** Lambda 関数 (Interactor, Executor) の環境変数を以下の通り設定する。
    - `Factorio_Executor`: `INSTANCE_ID`, `RCON_PORT`, `REGION`, `DYNAMODB_TABLE_NAME`, `S3_BUCKET_NAME`, `SAVE_FILE_KEY` (デフォルトは `save.zip`)
    - `Factorio_Interactor`: `DISCORD_PUBLIC_KEY`
4. **[ローカル]** `mkdir python && pip install pynacl -t ./python && zip -r pynacl_layer.zip python` を実行してレイヤーを作成する。
4. **[AWSコンソール]** `pynacl_layer.zip` を Lambda レイヤーとして登録し、`Factorio_Interactor` に適用する。
7. **[AWSコンソール]** EventBridge Scheduler を作成し、`Factorio_Executor` を以下の JSON ペイロードで呼び出すように設定する。
    - **無人監視 (5分おき)**: `{"action": "auto-check"}`
    - **定時停止 (毎日深夜など)**: `{"action": "stop"}`

### 3. サーバー：EC2 側及びデータの初期化
1. **[AWSコンソール]** セキュリティグループで、Lambda/VPC からの **TCP 27015** (RCON) および **UDP 34197** (Factorio) を許可する。
2. **[SSH]** Factorio サーバーの起動オプションに `--rcon-port 27015 --rcon-password <パスワード>` を追加する。
3. **[AWSコンソール]** EC2 インスタンスに付与する IAM ロールを作成し、`aws/IAM/EC2-Factorio-Server-RolePolicy/policy.json` の権限を適用する。
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
1. **[AWSコンソール/CLI]** `aws/Lambda/Factorio_Interactor/lambda_function.py` のコードを親Lambdaに関数デプロイ。
2. **[AWSコンソール/CLI]** `aws/Lambda/Factorio_Executor/lambda_function.py` のコードを子Lambdaに関数デプロイ。
3. **[AWSコンソール]** 両方のLambda関数の実行ロールに `aws/IAM/FactorioControlPolicy/policy.json` を適用する。

## ⚠️ 運用上の注意
- **メンテナンス時の自動停止**: 
  サーバーのアップデートや設定変更などのメンテナンス作業を行う際は、必ず **EventBridge Scheduler のトリガー（無人監視・定時停止）を「無効 (Disable)」** にしてください。
  作業中にプレイヤーが 0 人の状態が続くと、自動停止ロジックが作動してインスタンスが強制的にシャットダウンされる可能性があります。

## ⚖️ License
MIT License
