import os
import subprocess
import sys
import shlex
from dotenv import dotenv_values
import itertools

def get_all_commits():
    """すべてのコミットハッシュを取得"""
    try:
        result = subprocess.run(
            ['git', 'rev-list', '--all'],
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout.splitlines()
    except subprocess.CalledProcessError:
        print("❌ Gitリポジトリが見つからないか、gitコマンドが失敗しました。")
        return []

def check_env_leaks():
    # スクリプトの場所を基準にプロジェクトルートを取得
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # 環境選択 (デフォルト: prod)
    env_arg = sys.argv[1] if len(sys.argv) > 1 else "prod"
    env_file = ".env" if env_arg == "prod" else f".env.{env_arg}"
    env_path = os.path.join(BASE_DIR, env_file)

    if not os.path.exists(env_path):
        print(f"❌ エラー: 設定ファイルが見つかりません: {env_path}")
        sys.exit(1)

    # .envファイルをロード (現在の環境変数を汚染しないよう dotenv_values を使用)
    config = dotenv_values(env_path)
    if not config:
        print(f"⚠️  警告: {env_file} から値を取得できませんでした。ファイルが空か、パスが正しいか確認してください。")
        return

    print(f"📖 {env_file} から {len(config)} 個の項目を読み込みました。")
    
    commits = get_all_commits()
    if not commits:
        print("🛑 検索可能なコミット履歴がありません。")
        return
    print(f"📜 {len(commits)} 個のコミットをスキャンします...")

    found_leaks = False
    
    # 1. 検索から除外する「値」 (秘密情報ではない一般的な小文字の値)
    ignored_values = {'true', 'false', 'none', 'null', 'default', 'ap-northeast-1'}

    # 2. 検索自体をスキップする .env の「キー」
    # テーブル名、関数名、ポート番号など、Gitに含まれていても問題ない項目を指定します
    ignored_keys = {
        'AWS_REGION', 'DYNAMODB_TABLE_NAME', 'S3_BUCKET_NAME', 'SAVE_FILE_KEY',
        'INTERACTOR_LAMBDA_NAME', 'EXECUTOR_LAMBDA_NAME', 'WORKER_LAMBDA_NAME', 
        'NOTIFIER_LAMBDA_NAME', 'RCON_PORT', 'FACTORIO_GAME_PORT',
        'INTERACT_POLICY_NAME', 'EXECUTE_POLICY_NAME', 'NOTIFY_POLICY_NAME',
        'SERVER_POLICY_NAME', 'REGIST_POLICY_NAME', 'WORK_POLICY_NAME',
        'EPHEMERAL_COMMAND_STRINGS', 'RESTRICTED_COMMAND_STRINGS', 'COMMAND_ROUTING',
        'S3_FILES_SYSTEM_ID', 'RCON_COMMAND_TIMEOUT_SECONDS', 'RCON_UNRESPONSIVE_THRESHOLD',
        'ZERO_PLAYER_THRESHOLD', 'S3_SYNC_WAIT_THRESHOLD_SECONDS', 
        'RCON_READY_CHECK_INTERVAL_SECONDS', 'RCON_READY_CHECK_MAX_ATTEMPTS',
        'AUTO_CHECK_SCHEDULE_NAME', 'DAILY_STOP_SCHEDULE_NAME', 'EC2_STATE_RULE_NAME',
        'SSM_PARAMETER_PATH', 'AUTO_CHECK_SCHEDULE', 'DAILY_STOP_CRON', 
        'AWS_PROFILE', 'AWS_ACCOUNT_ID', 'COMMON_LAYER_NAME'
    }

    # 3. 検索から除外する「Git内のパス」 (Git pathspec)
    # ヒットしても無視したいファイルやディレクトリを指定します
    ignored_pathspecs = [
        '.',                # カレントディレクトリ以下すべてを対象
        ':!README.md',       # README.md を除外
        ':!.env.example',    # .env.example を除外
        ':!docs/*',          # docs フォルダを除外
        ':!*.example',       # その他 .example ファイルを除外
        ':!aws/IAM/*.json',  # 生成済みのポリシーファイルを除外
        ':!.deploy_state/*'  # デプロイ状態管理ファイルを除外
    ]

    # Windowsのコマンドライン文字数制限対策のため、コミットを分割して処理する
    CHUNK_SIZE = 50 
    commit_chunks = [commits[i:i + CHUNK_SIZE] for i in range(0, len(commits), CHUNK_SIZE)]

    for key, value in config.items():
        # 除外対象のキー、または値が空/短すぎる/一般的すぎる場合はスキップ
        if key in ignored_keys or not value or len(value) < 4 or value.lower() in ignored_values:
            continue

        print(f"🔍 [{key}] を検索中...")
        hit_files = set()

        for chunk in commit_chunks:
            # -F: 固定文字列として検索 (正規表現による誤認を防ぐ)
            # -l: 一致したファイル名のみ表示
            # 末尾に -- と pathspec を渡すことで検索範囲を制限
            cmd = ['git', 'grep', '-F', '-l', value] + chunk + ['--'] + ignored_pathspecs

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True
            )

            if result.stdout:
                hit_files.update(result.stdout.splitlines())
            
            if result.stderr and "argument list too long" in result.stderr.lower():
                print(f"❌ コマンドが長すぎます。CHUNK_SIZE を下げてください。")

        if hit_files:
            print(f"⚠️  【警告】'{key}' の値が履歴内で見つかりました:")
            for f in sorted(list(hit_files)):
                print(f"     📄 {f}")
            found_leaks = True

    if found_leaks:
        print("\n❌ 調査完了: いくつかの機密情報がGit履歴に含まれている可能性があります。")
        print("修正するには、'git filter-repo' や 'BFG Repo-Cleaner' などの使用を検討してください。")
    else:
        print("\n✅ 調査完了: 機密情報の漏洩は見つかりませんでした。")

if __name__ == "__main__":
    try:
        check_env_leaks()
    except KeyboardInterrupt:
        print("\n🛑 中断されました。")