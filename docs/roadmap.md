📝 Factorio Server Manager 開発ロードマップ (v2.0)
🏗️ Step 1: サーバー基盤と自動運用の確立（完了）
基本的な「動く仕組み」と、コスト削減のための自動停止ロジックを構築。

[x] サーバー可視化: /status コマンドによる EC2 状態確認。

[x] 停止時データ保護: RCON 連携によるセーブ命令発行後の安全な停止。

[x] 自動運用: プレイヤーゼロ継続時の自動シャットダウン、定時停止。

🛡️ Step 2: ステートレス・アーキテクチャと信頼性向上（完了）
Amazon S3 Files の導入と、4層構造（Interactor/Executor/Worker/Notifier）への刷新。

[x] 4層分離: 責務を完全に分け、非同期通知（Followup/POST）を実現。

[x] ストレージ革新: S3 Files によるセーブデータのマウントとバージョニング。

[x] 環境分離 (Dev/Prod): -dev サフィックスによる AWS リソースの完全分離と .env.dev 運用。

[x] 品質保証: test_runner.py による Lambda 統合テストと自動レポート。

[x] セキュリティ: 特定ユーザーへの権限制限（/restore 等）とパスワード保護。

📦 Step 3: 完全ステートレス化とリモート管理（現行フェーズ）
EC2 内のデータをすべて S3 へ逃がし、Discord からサーバー設定をフルコントロールする。

[ ] S3 ディレクトリ統合:

/saves, /mods, /config, /logs を S3 バケットへ完全集約。

EC2 起動時の自動マウント・リンク設定の最適化。

[ ] Discord リモート管理機能:

/config: server-settings.json の閲覧と動的な設定変更。

/mods: MOD リストの確認と mod-list.json による有効化切替。

/admin: 管理者リスト（admin-list, whitelist）の Discord 上での編集。

/log: 稼働ログの特定行抽出と Discord への送信。

🤖 Step 4: AI プレイヤー・エコシステム
Factorio 内で活動する AI との高度な連携。

[ ] AI 連携基盤: AI 専用サーバー（別環境）との通信 API 実装。

[ ] 連動シャットダウン: Factorio 停止に合わせた AI インスタンスの正常終了。