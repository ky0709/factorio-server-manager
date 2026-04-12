📝 Factorio Server Manager 開発ロードマップ
Phase 1: サーバー状態の可視化 (/status)
[x] feature/status-command ブランチの作成

[x] register.py の更新: /status コマンドの定義と登録

[x] 親Lambda (Interactions) の修正: status アクションを認識し、子へ非同期に渡すロジックの追加

[x] 子Lambda (Executor) の修正: ec2.describe_instances を用いた状態取得と、Discordへの整形メッセージ送信

[x] 動作確認: Discord上で /status が正しく現在のEC2状態を返すかテスト

[x] Pull Request 作成 & develop へマージ

Phase 2: サーバー停止時のデータ整合性確保
[x] feature/safe-save-on-stop ブランチの作成

[x] RCON (Remote Console) の導入: EC2内のFactorioへセーブ命令を送るための設定

[x] 停止ロジックの改善: EC2を止める直前に server-save コマンドを発行し、保存完了を確認してからインスタンスを停止する処理の実装

[x] Pull Request 作成 & develop へマージ

Phase 3: 自動停止・定時停止機能
[x] feature/auto-stop ブランチの作成

[x] EC2内監視スクリプトの作成: プレイヤー数を取得するPythonスクリプトの作成

[x] EventBridge (Scheduler) の設定:

[x]毎日指定時刻に停止Lambdaを叩く設定

[x]5分おきに監視Lambdaを叩く設定

[x] Lambdaの更新: プレイヤー0人が一定時間続いた場合の停止ロジック実装

[x] Pull Request 作成 & develop へマージ

Phase 4: セーブデータ管理と自動バックアップ (S3 Files 連携)

**初期ステップ**: 現在のセーブデータフォルダおよびログフォルダを Amazon S3 Files にマウントする。

**Amazon S3 Files 採用**: 2026年4月7日に発表された Amazon S3 Files を採用。従来の EFS や手動同期スクリプトを排除し、S3 のコストメリットとファイルシステムの利便性を両立させた最新のストレージ戦略を実装予定。

⚠️ **実装時の注意点（要確認）**:
発表されたばかりなので、以下の点を公式ドキュメントで確認する必要があります：
- 書き込みの遅延（レイテンシ）: Factorioのオートセーブ時にゲームが止まらない程度の速度が出るか。

[x] feature/save-data-management ブランチの作成

[x] S3バケットの作成とIAMポリシー更新: Lambda/EC2のS3アクセス権限追加

[x] IAMポリシーのテンプレート化と自動生成スクリプト (setup_config.py) の実装

[x] S3 バケットのバージョニング設定:

[x] /save コマンドの実装: サーバーを停止せずにRCON経由でセーブを実行する機能

[x] /restore コマンドの実装: S3バージョニングを利用した過去データのリスト表示と復元機能

[x] Pull Request 作成 & develop へマージ

Phase 4.5: アーキテクチャの最適化とセキュリティ強化
[x] Factorio_Notifier の新規作成と責務の委譲

[x] 非同期通知フローへの移行 (初期応答の PATCH 更新と Followup POST の使い分け)

[x] 権限バリデーションの実装 (ADMIN_USER_IDS/ROLE_IDS 照合および Discord UI 制限)

[x] エラーハンドリング（404/400リトライ、文字数制限切り詰め、同期状態表示）の共通化

Phase 4.7: 運用安定化と自動テスト
[x] 共通レイヤー (factorio_common) の導入と設定取得の一括化 (GetParametersByPath)
[x] test_runner.py の開発: モックデータを用いた全 Lambda 関数の統合テスト実装
[x] ログチャットへの自動テストレポート送信機能の実装

Phase 4.8: セキュリティ強化と稼働信頼性の向上
[x] ゲームパスワードの安全性向上（サーバー起動毎のパスワードリセット、Discord上でのマスク表示）
[x] 異常状態の検知と自動復旧: RCON 無応答時の自動再起動ロジックおよび無人停止通知の改善
[x] README.mdおよびドキュメント類の内容整理

Phase 5: AIプレイヤー連携
[ ] feature/ai-player-integration ブランチの作成

[ ] AI用サーバー（別環境）との連携APIの実装

[ ] 連動停止ロジック: Factorio停止時にAIサーバーへ終了リクエストを送る処理

[ ] Pull Request 作成 & develop へマージ