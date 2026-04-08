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

[ ] feature/save-data-management ブランチの作成

[ ] S3バケットの作成とIAMポリシー更新: Lambda/EC2のS3アクセス権限追加

[ ] アップロード用CLIツールの作成: ローカルからS3へ送るスクリプト

[ ] SSM Run Command の連携: S3更新を検知してEC2がファイルをプルする仕組み

[ ] 日付付き自動バックアップ実装:

定時停止時にセーブデータにサフィックス（例: _20260408）を付与

S3へ自動アップロードするスクリプトをEC2内に実装

[ ] /save-load コマンドの追加: Discordからファイル名を指定して切り替える機能

[ ] Pull Request 作成 & develop へマージ

Phase 5: AIプレイヤー連携
[ ] feature/ai-player-integration ブランチの作成

[ ] AI用サーバー（別環境）との連携APIの実装

[ ] 連動停止ロジック: Factorio停止時にAIサーバーへ終了リクエストを送る処理

[ ] Pull Request 作成 & develop へマージ