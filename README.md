# Factorio Server Manager (Serverless Edition)

Discordのスラッシュコマンドから、AWS上のFactorio専用サーバー（EC2）を起動・停止・状態確認するためのサーバーレスアプリケーションです。

今後の予定については [docs/roadmap.md](docs/roadmap.md) を参照してください。

## 🛠 特徴
- **完全サーバーレス**: Discordからのリクエストを AWS Lambda + API Gateway で直接処理するため、管理用の常駐サーバーが不要です。
- **非同期アーキテクチャ**: Discordの「3秒ルール（応答制限）」を回避するため、受付用（Interactions）と実行用（Executor）の親子Lambda構成を採用しています。
- **セキュリティ**: Discordからのリクエスト署名検証（Ed25519）を実装し、不正なアクセスを遮断します。
- **低コスト**: サーバーを利用しない時間はEC2を停止状態に保ち、運用コストを最小限に抑えます。

## 🏗 システム構成


1. **Discord User**: `/start` または `/stop` コマンドを実行
2. **API Gateway**: リクエストを親Lambdaへ転送
3. **Parent Lambda (Interactions)**: 署名検証を行い、即座に「受付完了」をDiscordへ返信。同時に子Lambdaを非同期で起動
4. **Child Lambda (Executor)**: EC2の起動/停止を操作し、完了後にDiscordのメッセージを更新（PATCH）

## 🚀 技術スタック
- **Language**: Python 3.12
- **Infrastructure**: AWS (Lambda, API Gateway, EC2, IAM)
- **Library**: 
  - `boto3` (AWS SDK)
  - `PyNaCl` (Signature Verification)
  - `requests`, `python-dotenv`

## 📁 フォルダ構成
- `aws/`: AWS関連の設定ファイル
  - `IAM/`: 最小権限の原則（Least Privilege）に基づくポリシー設定
  - `Lambda/`: 各関数のソースコード
- `register.py`: Discordへスラッシュコマンドを登録するためのユーティリティ
- `requirements.txt`: ローカル環境用ライブラリ

## 📝 セットアップ
1. `.env.example` を参考に `.env` を作成し、各種トークンとインスタンスIDを設定。
2. `pip install -r requirements.txt` で依存関係をインストール。
3. `python register.py` を実行してDiscordにコマンドを登録。
4. **AWS Lambda レイヤーの準備**
   
   署名検証ライブラリ `PyNaCl` は Lambda の標準環境に含まれないため、以下の手順でレイヤーを作成・適用してください。
   ```bash
   mkdir python
   pip install pynacl -t ./python
   zip -r pynacl_layer.zip python
   ```
   作成した pynacl_layer.zip を AWS Lambda のレイヤーとして登録し、Factorio_Interactions 関数にアタッチします。
5. ソースコードのデプロイ
aws/Lambda/ 内の各 lambda_function.py をそれぞれの関数にデプロイしてください。

## ⚖️ License
MIT License
