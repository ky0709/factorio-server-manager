# Factorio Server Manager (Serverless Edition)

Discordのスラッシュコマンドから、AWS上のFactorio専用サーバー（EC2）を起動・停止・状態確認するためのサーバーレスアプリケーションです。

今後の予定については [docs/roadmap.md](docs/roadmap.md) を参照してください。

## 🛠 特徴
- **完全サーバーレス**: Discordからのリクエストを AWS Lambda + API Gateway で直接処理するため、管理用の常駐サーバーが不要です。
- **非同期アーキテクチャ**: Discordの「3秒ルール（応答制限）」を回避するため、受付用（Interactions）と実行用（Executor）の親子Lambda構成を採用しています。
- **多重系の停止ロジック (Robust Shutdown)**: 動的な「無人検知（アプリ層）」と静的な「定時停止（インフラ層）」を組み合わせることで、ログアウト忘れやアプリケーションの不備に左右されない、極めて堅牢なコスト最適化を実現しています。
- **セキュリティ**: Discordからのリクエスト署名検証（Ed25519）を実装し、不正なアクセスを遮断します。
- **機密情報の自動同期**: ローカルの `.env` に記載した機密情報を、コマンド登録時に AWS SSM へ自動的に同期・アップロードします。

## 🏗 システム構成
1. **Discord User**: `/start` / `/stop` / `/status` コマンドを実行
2. **API Gateway**: Discordからのリクエストを Interactions Lambda へ転送
3. **Interactions Lambda (Parent)**: 署名検証を行い、即座に「受付完了」を返信。Executor Lambda を非同期で起動
4. **Executor Lambda (Child)**: 
    - **コマンド実行**: EC2の起動/停止、RCON経由のセーブを実行。完了後に Discord の初期応答を PATCH で更新
    - **定期監視**: EventBridgeからのトリガーを受け、プレイヤー数をチェック。無人状態が続けば自動停止を実行
5. **DynamoDB**: 無人状態の継続回数を保持
6. **SSM Parameter Store**: RCONパスワードやDiscord Webhook URLなどの機密情報を安全に保持

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

##  フォルダ構成
- `aws/`: AWS関連の設定ファイル
  - `IAM/`: 最小権限の原則（Least Privilege）に基づくポリシー設定
  - `Lambda/`: `Interactions`（受付用）と `Executor`（実行用）のソースコード
- `docs/`: 開発ロードマップ等
- `register.py`: Discordコマンドの登録および機密情報（SSM）の同期を行うユーティリティ
- `.env`: ローカル環境用の認証情報およびAWS同期用設定（Git管理対象外）
- `requirements.txt`: ローカル環境用ライブラリ

## 📝 セットアップ

### 1. ローカル：機密情報の準備と同期
1. **[AWSコンソール]** IAM ユーザー `FactorioRegistUser` を作成し、`aws/IAM/FactorioRegistPolicy/policy.json` の権限を付与してアクセスキーを発行する。
2. **[ローカル]** `.env.example` をコピーして `.env` を作成し、取得したアクセスキーや Discord 認証情報を入力する。
3. **[ローカル]** `pip install -r requirements.txt` を実行して依存ライブラリをインストールする。
4. **[ローカル]** `python register.py` を実行し、Discord コマンドの登録と SSM Parameter Store への機密情報同期を行う。

### 2. AWS：インフラリソースの構築
1. **[AWSコンソール]** DynamoDB テーブル `FactorioState` を作成する（パーティションキー: `ConfigKey` (文字列)）。
2. **[AWSコンソール]** Lambda 関数 (`Interactions`, `Executor`) の環境変数を以下の通り設定する。
    - `Factorio_Executor`: `INSTANCE_ID`, `RCON_PORT`, `REGION`, `DYNAMODB_TABLE_NAME`
    - `Factorio_Interactions`: `DISCORD_PUBLIC_KEY`
3. **[ローカル]** `mkdir python && pip install pynacl -t ./python && zip -r pynacl_layer.zip python` を実行してレイヤーを作成する。
4. **[AWSコンソール]** `pynacl_layer.zip` を Lambda レイヤーとして登録し、`Factorio_Interactions` に適用する。
5. **[AWSコンソール]** EventBridge Scheduler を作成し、`Factorio_Executor` を以下の JSON ペイロードで呼び出すように設定する。
    - **無人監視 (5分おき)**: `{"action": "auto-check"}`
    - **定時停止 (毎日深夜など)**: `{"action": "stop"}`

### 3. サーバー：EC2 側及びデータの初期化
1. **[AWSコンソール]** セキュリティグループで、Lambda/VPC からの **TCP 27015** (RCON) および **UDP 34197** (Factorio) を許可する。
2. **[SSH]** Factorio サーバーの起動オプションに `--rcon-port 27015 --rcon-password <パスワード>` を追加する。
3. **[AWSコンソール]** DynamoDB の `FactorioState` テーブルに、初期データとして `{ "ConfigKey": "ZeroPlayerCount", "CountValue": 0 }` を手動で作成する。

## 🚀 デプロイ
1. **[AWSコンソール/CLI]** `aws/Lambda/Factorio_Interactions/lambda_function.py` のコードを親Lambdaに関数デプロイ。
2. **[AWSコンソール/CLI]** `aws/Lambda/Factorio_Executor/lambda_function.py` のコードを子Lambdaに関数デプロイ。
3. **[AWSコンソール]** 両方のLambda関数の実行ロールに `aws/IAM/FactorioControlPolicy/policy.json` を適用する。

## ⚖️ License
MIT License
