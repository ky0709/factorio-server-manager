import subprocess
import sys
import os
import argparse

def run_script(script_path, env):
    """ヘルパースクリプトを実行する"""
    print(f"\n{'='*60}")
    print(f"🚀 Executing: {script_path} {env}")
    print(f"{'='*60}")
    
    # Pythonインタープリタのパスを取得
    python_exe = sys.executable
    
    try:
        result = subprocess.run([python_exe, script_path, env], check=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ Error during execution of {script_path}: {e}")
        return False

def get_git_hash():
    """現在のGitコミットハッシュを取得"""
    try:
        return subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    except Exception:
        return None

def get_git_ref():
    """現在のGitブランチ名またはハッシュを取得"""
    try:
        # まずブランチ名の取得を試みる
        ref = subprocess.check_output(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], text=True).strip()
        # デタッチ状態（HEAD）ならハッシュを取得
        if ref == 'HEAD':
            ref = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
        # 改行などの不要な文字を除去
        return ref.strip()
    except Exception:
        print("⚠️  Warning: Git reference could not be determined.")
        return None

def is_dirty():
    """ワーキングディレクトリに未コミットの変更があるかチェック"""
    try:
        status = subprocess.check_output(['git', 'status', '--porcelain'], text=True).strip()
        return len(status) > 0
    except Exception:
        return False

def main():
    # スクリプトの場所を基準にプロジェクトルートへ移動
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(project_root)

    # 現在のコミットハッシュとリファレンスを保存
    current_commit = get_git_hash()
    original_ref = get_git_ref()

    # ステップのニックネームとスクリプトパスのマッピング
    all_scripts = [
        ("config", "scripts/setup_config.py"),
        ("policies", "scripts/deploy_policies.py"),
        ("layer", "scripts/update_layer.py"),
        ("lambda", "scripts/deploy_lambda.py"),
        ("register", "scripts/register.py"),
        ("schedule", "scripts/update_schedule.py"),
        ("test", "scripts/test_runner.py")
    ]
    valid_steps = [name for name, _ in all_scripts]

    parser = argparse.ArgumentParser(description="Factorio Server Manager Full Deployment Pipeline")
    parser.add_argument("env", nargs="?", default="prod", help="デプロイ先の環境 (dev/prod)")
    parser.add_argument("--skip", nargs="+", choices=valid_steps, help="スキップするステップを指定")
    parser.add_argument("--only", nargs="+", choices=valid_steps, help="実行する特定のステップだけを指定")
    parser.add_argument("--rollback", action="store_true", help="失敗時に自動で cleanup_aws_resources.sh を実行する")
    parser.add_argument("--git-rollback", action="store_true", help="失敗時に前回の成功コミットにチェックアウトして再試行する")

    args = parser.parse_args()
    env = args.env
    skip_list = args.skip if args.skip else []
    only_list = args.only if args.only else []

    # デプロイ状態を管理するディレクトリの作成
    state_dir = os.path.join(project_root, ".deploy_state")
    os.makedirs(state_dir, exist_ok=True)
    success_marker = os.path.join(state_dir, f"last_success_{env}")

    # 1. 実行確認 (AUTO_CONFIRM が設定されていない場合のみ)
    if os.getenv('AUTO_CONFIRM') != '1':
        if env == 'prod':
            print(f"🚨 ATTENTION: You are about to run a FULL deployment for the PRODUCTION environment.")
            confirm = input(f"Proceed with full deployment for 'prod'? (y/N): ")
            if confirm.lower() != 'y':
                print("🛑 Operation cancelled.")
                sys.exit(1)
            
            prod_confirm = input("⚠️  FINAL CONFIRMATION: To proceed, please type 'DEPLOY-PROD': ")
            if prod_confirm != 'DEPLOY-PROD':
                print("🛑 Production deployment aborted.")
                sys.exit(1)
        else:
            confirm = input(f"Proceed with full deployment for '{env}'? (y/N): ")
            if confirm.lower() != 'y':
                print("🛑 Operation cancelled.")
                sys.exit(1)
    else:
        print(f"🤖 Auto-confirm enabled. Proceeding with [{env}] deployment...")

    # 2. 以降の子スクリプトで個別の確認プロンプトを出さないように設定
    os.environ['AUTO_CONFIRM'] = '1'
    
    print(f"🏁 Starting Full Deployment Pipeline for [{env}]")
    if only_list:
        print(f"🎯 Running only: {', '.join(only_list)}")
    elif skip_list:
        print(f"⏭️  Skipping steps: {', '.join(skip_list)}")
    
    for name, script in all_scripts:
        # --only が指定されている場合、含まれていないステップは飛ばす
        if only_list and name not in only_list:
            continue
        # --only が指定されていない場合に限り、--skip を適用する
        if not only_list and name in skip_list:
            continue

        if not os.path.exists(script):
            print(f"⚠️  Warning: {script} not found, skipping...")
            continue
            
        if not run_script(script, env):
            print(f"\n🛑 Pipeline failed at {script}.")
            
            # Git ロールバック処理
            if args.git_rollback and os.environ.get('IS_GIT_ROLLBACK') != '1':
                if os.path.exists(success_marker):
                    with open(success_marker, 'r') as f:
                        last_commit = f.read().strip()
                    
                    # ロールバック条件: 1. コミットが異なる, 2. コミットは同じだが未コミットの変更(dirty)がある
                    needs_rollback = last_commit and (last_commit != current_commit or is_dirty())

                    if needs_rollback:
                        print(f"\n⏪ Rolling back to last successful commit: {last_commit}")
                        
                        stashed = False
                        try:
                            # 未コミットの変更があればスタッシュに退避
                            if is_dirty():
                                print("📦 Uncommitted changes detected. Stashing for safety...")
                                subprocess.run(['git', 'stash', 'push', '-m', f'deploy_all auto-stash before rollback to {last_commit}'], check=True)
                                stashed = True

                            subprocess.run(['git', 'checkout', last_commit], check=True)
                            
                            # 再起動前に自分自身が存在するかチェック
                            if not os.path.exists(sys.argv[0]):
                                print(f"❌ Rollback failed: The script {sys.argv[0]} does not exist in the previous commit.")
                                print("Please restore the environment manually.")
                                sys.exit(1)

                            print("✅ Checkout successful. Re-starting deployment pipeline...")
                            # 環境変数をセットして再帰的に実行 (無限ループ防止)
                            os.environ['IS_GIT_ROLLBACK'] = '1'
                            # 元の引数を引き継いで再実行
                            cmd = [sys.executable, sys.argv[0], env] + sys.argv[2:]
                            subprocess.run(cmd)
                            
                            # 処理完了後、元のブランチに戻す
                            if original_ref:
                                print(f"\n🔄 Returning to original ref: {original_ref}")
                                subprocess.run(['git', 'checkout', original_ref], check=True)
                                
                                if stashed:
                                    print("📦 Restoring stashed changes...")
                                    subprocess.run(['git', 'stash', 'pop'], check=True)

                            sys.exit(1)
                        except Exception as e:
                            print(f"❌ Failed to perform git rollback: {e}")
                else:
                    print(f"⚠️  No previous success marker found at {success_marker}. Cannot rollback.")

            # AWS リソースのクリーンアップ (既存機能)
            if args.rollback:
                print(f"\n🧨 Rollback (Cleanup) initiated as requested...")
                cleanup_script = "scripts/cleanup_aws_resources.sh"
                if os.path.exists(cleanup_script):
                    # cleanup_aws_resources.sh は入力を求めるため、確認文字列を流し込む
                    confirm_str = f"DELETE-{env}"
                    subprocess.run(["bash", cleanup_script, env], input=confirm_str, text=True)
                    print(f"✅ Environment [{env}] has been cleaned up.")
                else:
                    print(f"❌ Cleanup script not found at {cleanup_script}")
            
            sys.exit(1)
            
    # 3. 成功の記録
    if current_commit:
        try:
            with open(success_marker, 'w') as f:
                f.write(current_commit)
        except Exception as e:
            print(f"⚠️  Could not write success marker: {e}")

    print(f"\n🎉 Full Deployment for [{env}] completed successfully!")

if __name__ == "__main__":
    main()