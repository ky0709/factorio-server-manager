# Factorio Server Manager コマンドリファレンス

本システムで利用可能なスラッシュコマンドおよび自動実行機能のリファレンスです。
機能分離（Executor / Worker）後の最新の構成に基づいています。

## 1. ユーザーコマンド (Slash Commands)

| コマンド | サブコマンド | 担当 Lambda | 内容 |
| :--- | :--- | :--- | :--- |
| `/status` | - | Executor | サーバー(EC2)の起動状態、Factorioサーバーのオンライン状態、接続先、プレイヤー名、セーブ日時（S3同期状態を含む）およびファイル容量を表示します。 |
| `/start` | - | Executor | Factorioサーバーを起動します。 |
| `/stop` | - | Executor | セーブを実行し、Factorioサーバーを停止します。 |
| `/save` | - | Executor | RCON経由でゲームのセーブを実行し、S3への反映待ち状態を DynamoDB に記録します。 |
| `/pass` | - | Executor | SSM Parameter Store に保存されているゲーム参加用パスワードを表示します。 |
| `/license` | - | Executor | 本ソフトウェアの MIT ライセンス情報を表示します。 |
| `/restore` | `list` | **Worker** | S3 履歴を表示します（最大15件）。同期中の場合はカタログ上の最新時刻が警告として表示されます。 |
| `/restore` | `select` | **Worker** | 指定した Version ID のデータを S3 上でカレントとして復元します。※サーバー停止中。 |

## 2. 自動実行機能 (Background Tasks)

これらの機能は EventBridge Scheduler によって定期的に実行されます。

### 無人停止チェック (`auto-check`)
- **担当**: Factorio_Worker
- **トリガー**: 5分間隔（推奨）
- **内容**: 
    1. RCON で現在のプレイヤー数を確認。
    2. プレイヤー 0 人の状態が設定値（`ZERO_PLAYER_THRESHOLD`）以上続いた場合、`/stop` アクションを Executor に依頼します。
    3. RCON 通信が失敗（応答なし）の状態が設定値（`RCON_UNRESPONSIVE_THRESHOLD`）以上続いた場合、SSM 経由で Factorio サービスの再起動を試みます。

### 定時停止
- **担当**: Factorio_Executor
- **内容**: 深夜帯などの決まった時刻に、無条件でサーバーを停止させます。
- **ログ**: EC2 が実際に停止（Stopped）したタイミングで、所要時間を含めた詳細ログが管理チャンネルに送信されます。

## 3. 技術的詳細

### メッセージの更新 (PATCH)
以下の参照系コマンドは、Interactor が出した初期応答「確認中...」を、Executor/Worker が処理完了後に直接書き換えます。これにより Discord のログが汚れず、スムーズな UI を提供します。
- `status`, `pass`, `license`, `restore list`

### 権限管理
デフォルトでは `/restore select` 等が制限対象に設定されています。制限対象のコマンドは、SSM の `ADMIN_USER_IDS` または `ADMIN_ROLE_IDS` に含まれるユーザーのみが実行可能です。制限の追加・削除は `.env` の `RESTRICTED_COMMAND_STRINGS` から行え、`register.py` 実行時に Discord 上の説明文へ注釈が自動で追記されます。

### 状態管理 (DynamoDB)
`FactorioState` テーブルを使用して以下の状態を管理しています。
- `OfflineCount`: RCON 応答不可の連続回数
- `ZeroPlayerCount`: プレイヤー 0 人の連続回数
- `LatestSaveInfo`: 最後にセーブ命令を出した時刻とファイル容量（S3同期状態の判定に使用）
- `StartStartTime` / `StopStartTime`: 処理時間を計測するための実行開始タイムスタンプ

---
*Created by Gemini Code Assist - 2026/04/11*