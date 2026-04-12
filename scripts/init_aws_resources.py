import os
import boto3
import sys
import json
import zipfile
import io
import time
import subprocess
from botocore.exceptions import ClientError
from dotenv import load_dotenv

def main():
    # プロジェクトルートの取得
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # 環境選択 (デフォルト: prod)
    env_arg = sys.argv[1] if len(sys.argv) > 1 else "prod"
    env_file = f".env.{env_arg}" if env_arg != "prod" else ".env"
    env_path = os.path.join(BASE_DIR, env_file)

    if not os.path.exists(env_path):
        print(f"❌ Environment file {env_file} not found.")
        sys.exit(1)

    load_dotenv(env_path)
    
    # 個別実行時、または deploy_all --only init の場合は setup_config.py を事前に実行
    if os.getenv('SETUP_CONFIG_DONE') != '1':
        print(f"\n⚙️  Generating configuration files from templates ({env_arg})...")
        python_exe = sys.executable
        setup_script = os.path.join(BASE_DIR, "scripts/setup_config.py")
        try:
            subprocess.run([python_exe, setup_script, env_arg], check=True)
            print("✅ setup_config.py completed.")
        except subprocess.CalledProcessError as e:
            print(f"❌ Error during execution of setup_config.py: {e}")
            sys.exit(1)

    region = os.getenv('AWS_REGION', 'ap-northeast-1').strip("'\" ")
    account_id = os.getenv('AWS_ACCOUNT_ID', '').strip("'\" ")
    bucket_name = os.getenv('S3_BUCKET_NAME', '').strip("'\" ")
    table_name = os.getenv('DYNAMODB_TABLE_NAME', '').strip("'\" ")
    instance_id = os.getenv('INSTANCE_ID', '').strip("'\" ")

    # ロール・リソース名の決定 (prod の場合はサフィックスなし)
    suffix = f"-{env_arg}" if env_arg == "dev" else ""
    auto_check_name = os.getenv('AUTO_CHECK_SCHEDULE_NAME', f"Factorio-AutoCheck{suffix}")
    daily_stop_name = os.getenv('DAILY_STOP_SCHEDULE_NAME', f"Factorio-DailyStop{suffix}")
    rule_name = os.getenv('EC2_STATE_RULE_NAME', f"Factorio-EC2StateChange{suffix}")

    # --- Naming Convention & Format Validation ---
    errors = []
    
    # S3 Prefix Check
    if bucket_name and not bucket_name.startswith('factorio-'):
        errors.append(f"S3_BUCKET_NAME must start with 'factorio-' (current: {bucket_name})")
    
    # General "Factorio" Prefix Check
    naming_targets = {
        'DYNAMODB_TABLE_NAME': table_name,
        'AUTO_CHECK_SCHEDULE_NAME': auto_check_name,
        'DAILY_STOP_SCHEDULE_NAME': daily_stop_name,
        'EC2_STATE_RULE_NAME': rule_name,
        'INTERACTOR_LAMBDA_NAME': os.getenv('INTERACTOR_LAMBDA_NAME'),
        'EXECUTOR_LAMBDA_NAME': os.getenv('EXECUTOR_LAMBDA_NAME'),
        'WORKER_LAMBDA_NAME': os.getenv('WORKER_LAMBDA_NAME'),
        'NOTIFIER_LAMBDA_NAME': os.getenv('NOTIFIER_LAMBDA_NAME'),
        'INTERACT_POLICY_NAME': os.getenv('INTERACT_POLICY_NAME'),
        'EXECUTE_POLICY_NAME': os.getenv('EXECUTE_POLICY_NAME'),
        'NOTIFY_POLICY_NAME': os.getenv('NOTIFY_POLICY_NAME'),
        'SERVER_POLICY_NAME': os.getenv('SERVER_POLICY_NAME'),
        'REGIST_POLICY_NAME': os.getenv('REGIST_POLICY_NAME'),
        'WORK_POLICY_NAME': os.getenv('WORK_POLICY_NAME'),
        'SCHEDULE_POLICY_NAME': os.getenv('SCHEDULE_POLICY_NAME')
    }
    
    for key, val in naming_targets.items():
        if val and not val.startswith('Factorio'):
            errors.append(f"{key} must start with 'Factorio' (current: {val})")

    # AWS ID Format Checks
    if account_id and (not account_id.isdigit() or len(account_id) != 12):
        errors.append(f"AWS_ACCOUNT_ID must be a 12-digit number (current: {account_id})")
    
    if instance_id and not instance_id.startswith('i-'):
        errors.append(f"INSTANCE_ID must start with 'i-' (current: {instance_id})")
    
    if not os.getenv('AWS_PROFILE'):
        print("⚠️  Warning: AWS_PROFILE is not set in .env. Using default credentials.")

    if errors:
        print("\n❌ Configuration Validation Failed:")
        for err in errors:
            print(f"  - {err}")
        print("\nPlease fix these in your .env file before proceeding.")
        sys.exit(1)

    # IAMロール名の決定
    role_mapping = {
        'executor': f"{os.getenv('EXECUTOR_ROLE_NAME', 'FactorioExecutorRole')}{suffix}",
        'worker': f"{os.getenv('WORKER_ROLE_NAME', 'FactorioWorkerRole')}{suffix}",
        'notifier': f"{os.getenv('NOTIFIER_ROLE_NAME', 'FactorioNotifierRole')}{suffix}",
        'interactor': f"{os.getenv('INTERACTOR_ROLE_NAME', 'FactorioInteractorRole')}{suffix}"
    }

    base_eventbridge_role = os.getenv('EVENTBRIDGE_ROLE_NAME', 'FactorioEventBridgeRole')
    eventbridge_role_name = f"{base_eventbridge_role}{suffix}"

    print(f"🚀 Starting resource creation for [{env_arg.upper()}] in {region}")
    
    # 本番環境の場合の最終確認
    if env_arg == "prod" and os.getenv('AUTO_CONFIRM') != '1':
        print("🚨 ATTENTION: You are about to create PRODUCTION resources.")
        confirm = input("Proceed with production resource creation? (y/N): ")
        if confirm.lower() != 'y':
            print("🛑 Cancelled.")
            sys.exit(1)

        prod_confirm = input("⚠️  FINAL CONFIRMATION: To proceed, please type 'INIT-PROD': ")
        if prod_confirm != 'INIT-PROD':
            print("🛑 Production resource creation aborted.")
            sys.exit(1)

    # AWS Clients
    s3 = boto3.client('s3', region_name=region)
    dynamodb = boto3.client('dynamodb', region_name=region)
    iam = boto3.client('iam')
    awslambda = boto3.client('lambda', region_name=region)
    scheduler = boto3.client('scheduler', region_name=region)
    events = boto3.client('events', region_name=region)

    def call_iam_with_retry(func, **kwargs):
        """IAMの反映遅延(AccessDenied)対策のリトライ付き呼び出し"""
        max_attempts = 5
        for attempt in range(max_attempts):
            try:
                return func(**kwargs)
            except ClientError as e:
                if e.response['Error']['Code'] in ['AccessDenied', 'AccessDeniedException'] and attempt < max_attempts - 1:
                    wait_time = 2 ** (attempt + 1)
                    print(f"  ⚠️  IAM access denied. Policy propagation might be delayed. Retrying in {wait_time}s... ({attempt + 1}/{max_attempts})")
                    time.sleep(wait_time)
                    continue
                raise e

    # 1. S3 Bucket
    try:
        s3.head_bucket(Bucket=bucket_name)
        print(f"    (S3 Bucket {bucket_name} already exists)")
    except ClientError:
        print(f"📦 Creating S3 Bucket: {bucket_name}...")
        create_opts = {}
        if region != 'us-east-1':
            create_opts['CreateBucketConfiguration'] = {'LocationConstraint': region}
        s3.create_bucket(Bucket=bucket_name, **create_opts)
        s3.put_bucket_versioning(Bucket=bucket_name, VersioningConfiguration={'Status': 'Enabled'})
        print("✅ S3 Bucket created and Versioning enabled.")

    # 2. DynamoDB Table
    try:
        dynamodb.describe_table(TableName=table_name)
        print(f"    (DynamoDB Table {table_name} already exists)")
    except ClientError:
        print(f"📊 Creating DynamoDB Table: {table_name}...")
        dynamodb.create_table(
            TableName=table_name,
            AttributeDefinitions=[{'AttributeName': 'ConfigKey', 'AttributeType': 'S'}],
            KeySchema=[{'AttributeName': 'ConfigKey', 'KeyType': 'HASH'}],
            ProvisionedThroughput={'ReadCapacityUnits': 1, 'WriteCapacityUnits': 1}
        )
        print("📝 Initializing DynamoDB data...")
        
        # テーブルが利用可能になるまで待機 (5秒間隔で最大12回試行)
        waiter = dynamodb.get_waiter('table_exists')
        waiter.wait(TableName=table_name, WaiterConfig={'Delay': 5, 'MaxAttempts': 12})

        dynamodb.put_item(
            TableName=table_name,
            Item={'ConfigKey': {'S': 'ZeroPlayerCount'}, 'CountValue': {'N': '0'}}
        )
        print("✅ DynamoDB Table created and initialized.")

    # 3. IAM Roles (Lambda)
    print("👤 Checking IAM Roles for Lambda...")
    trust_path = os.path.join(BASE_DIR, "aws/IAM/FactorioLambdaRole/trust_policy.json")
    with open(trust_path, 'r') as f:
        trust_policy_str = f.read()

    for r_key, r_name in role_mapping.items():
        try:
            iam.get_role(RoleName=r_name)
            print(f"    (IAM Role {r_name} already exists)")
        except ClientError:
            print(f"  - Creating {r_name}...")
            iam.create_role(RoleName=r_name, AssumeRolePolicyDocument=trust_policy_str)
        
        # 基本実行権限のアタッチ
        basic_arn = 'arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'
        attached = call_iam_with_retry(iam.list_attached_role_policies, RoleName=r_name).get('AttachedPolicies', [])
        if any(p['PolicyArn'] == basic_arn for p in attached):
            print(f"    (Basic execution policy already attached to {r_name})")
        else:
            iam.attach_role_policy(RoleName=r_name, PolicyArn=basic_arn)

    # 4. Initial IAM Policies (Placeholders)
    policies = [
        os.getenv('INTERACT_POLICY_NAME'), os.getenv('EXECUTE_POLICY_NAME'),
        os.getenv('NOTIFY_POLICY_NAME'), os.getenv('SERVER_POLICY_NAME'),
        os.getenv('REGIST_POLICY_NAME'), os.getenv('WORK_POLICY_NAME'),
        os.getenv('SCHEDULE_POLICY_NAME')
    ]
    empty_policy = {"Version": "2012-10-17", "Statement": [{"Effect": "Deny", "Action": "*", "Resource": "*"}]}
    
    print("⚖️  Checking IAM Policies...")
    for p_name in policies:
        if not p_name: continue
        arn = f"arn:aws:iam::{account_id}:policy/{p_name}"
        try:
            iam.get_policy(PolicyArn=arn)
            print(f"    (Already exists: {p_name})")
        except ClientError:
            print(f"  - Creating {p_name}...")
            iam.create_policy(PolicyName=p_name, PolicyDocument=json.dumps(empty_policy))

    # 4.5. Attach Custom Policies to respective Roles
    print("🔗 Attaching custom policies to respective Lambda roles...")
    attachment_mapping = [
        {"role": role_mapping['executor'], "policy": os.getenv('EXECUTE_POLICY_NAME', 'FactorioExecutePolicy')},
        {"role": role_mapping['worker'], "policy": os.getenv('WORK_POLICY_NAME', 'FactorioWorkPolicy')},
        {"role": role_mapping['notifier'], "policy": os.getenv('NOTIFY_POLICY_NAME', 'FactorioNotifyPolicy')},
        {"role": role_mapping['interactor'], "policy": os.getenv('INTERACT_POLICY_NAME', 'FactorioInteractPolicy')}
    ]

    for attach in attachment_mapping:
        if not attach["policy"]: continue
        p_arn = f"arn:aws:iam::{account_id}:policy/{attach['policy']}"
        try:
            attached = call_iam_with_retry(iam.list_attached_role_policies, RoleName=attach['role']).get('AttachedPolicies', [])
            if any(p['PolicyArn'] == p_arn for p in attached):
                print(f"    (Already attached: {attach['policy']} to {attach['role']})")
            else:
                print(f"  - Attaching {attach['policy']} to {attach['role']}...")
                iam.attach_role_policy(RoleName=attach['role'], PolicyArn=p_arn)
        except ClientError as e:
            print(f"⚠️  Could not check/attach {attach['policy']}: {e}")

    # 5. Lambda Placeholders
    def get_env_int(key, default):
        val = os.getenv(key)
        if val:
            try:
                return int(val.strip().strip("'\""))
            except ValueError:
                print(f"⚠️  Warning: Invalid integer for {key}: {val}. Using default: {default}")
        return default

    lambda_configs = {
        os.getenv('EXECUTOR_LAMBDA_NAME'): {
            'MemorySize': get_env_int('EXECUTOR_MEMORY', 256),
            'Timeout': get_env_int('EXECUTOR_TIMEOUT', 180),
            'Role': role_mapping['executor']
        },
        os.getenv('NOTIFIER_LAMBDA_NAME'): {
            'MemorySize': get_env_int('NOTIFIER_MEMORY', 128),
            'Timeout': get_env_int('NOTIFIER_TIMEOUT', 30),
            'Role': role_mapping['notifier']
        },
        os.getenv('WORKER_LAMBDA_NAME'): {
            'MemorySize': get_env_int('WORKER_MEMORY', 256),
            'Timeout': get_env_int('WORKER_TIMEOUT', 60),
            'Role': role_mapping['worker']
        },
        os.getenv('INTERACTOR_LAMBDA_NAME'): {
            'MemorySize': get_env_int('INTERACTOR_MEMORY', 512),
            'Timeout': get_env_int('INTERACTOR_TIMEOUT', 10),
            'Role': role_mapping['interactor']
        }
    }

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w') as zf:
        zf.writestr('lambda_function.py', "def lambda_handler(event, context): return {'status': 'initializing'}")
    
    # 環境変数のベース (SSMパスなど)
    ssm_base = os.getenv('SSM_PARAMETER_PATH', '/factorio/').strip("'\" ")

    print("λ Checking Lambda functions...")
    for f_name, config in lambda_configs.items():
        if not f_name: continue
        target_role_arn = f'arn:aws:iam::{account_id}:role/{config["Role"]}'

        # 既存設定の取得
        current = None
        try:
            current = awslambda.get_function_configuration(FunctionName=f_name)
        except ClientError as e:
            if e.response['Error']['Code'] != 'ResourceNotFoundException':
                raise e

        # 指数バックオフによるリトライロジック (iam:PassRole 伝播待ち用)
        max_attempts = 5
        for attempt in range(max_attempts):
            try:
                if current:
                    # 更新が必要かチェック
                    if current['MemorySize'] != config['MemorySize'] or current['Timeout'] != config['Timeout'] or current['Role'] != target_role_arn:
                        print(f"  - Updating {f_name} config (Memory: {config['MemorySize']}, Timeout: {config['Timeout']}s, Role: {config['Role']})...")
                        awslambda.update_function_configuration(
                            FunctionName=f_name,
                            MemorySize=config['MemorySize'],
                            Timeout=config['Timeout'],
                            Role=target_role_arn
                        )
                    else:
                        print(f"    (Already exists with correct config: {f_name})")
                else:
                    # 新規作成
                    print(f"  - Creating {f_name}...")
                    awslambda.create_function(
                        FunctionName=f_name,
                        Runtime='python3.12',
                        Role=target_role_arn,
                        Handler='lambda_function.lambda_handler',
                        Code={'ZipFile': zip_buffer.getvalue()},
                        MemorySize=config['MemorySize'],
                        Timeout=config['Timeout'],
                        Environment={'Variables': {'SSM_PARAMETER_PATH': ssm_base}}
                    )
                break # 成功したらループを抜ける

            except ClientError as e:
                # iam:PassRole 権限の反映待ち (AccessDeniedException) の場合のみリトライ
                if e.response['Error']['Code'] == 'AccessDeniedException' and 'iam:PassRole' in str(e):
                    if attempt < max_attempts - 1:
                        wait_time = 2 ** (attempt + 1)
                        print(f"  ⚠️  AccessDenied (iam:PassRole). AWS policy propagation might be delayed. Retrying in {wait_time}s... ({attempt + 1}/{max_attempts})")
                        time.sleep(wait_time)
                        continue
                print(f"❌ Error processing {f_name}: {e}")
                raise e

    # 6. Scheduler IAM Role
    try:
        iam.get_role(RoleName=eventbridge_role_name)
        print(f"    (IAM Role {eventbridge_role_name} already exists)")
    except ClientError:
        print(f"👤 Creating IAM Role for EventBridge: {eventbridge_role_name}...")
        trust_path = os.path.join(BASE_DIR, "aws/IAM/FactorioEventBridgeRole/trust_policy.json")
        with open(trust_path, 'r') as f:
            trust_policy_str = f.read()
        iam.create_role(RoleName=eventbridge_role_name, AssumeRolePolicyDocument=trust_policy_str)
        print("✅ IAM Role for EventBridge created.")

    print(f"⚖️  Attaching managed policy to {eventbridge_role_name}...")
    eb_p_name = os.getenv('EVENTBRIDGE_POLICY_NAME', 'FactorioEventBridgePolicy')
    eb_p_arn = f"arn:aws:iam::{account_id}:policy/{eb_p_name}"
    
    attached = call_iam_with_retry(iam.list_attached_role_policies, RoleName=eventbridge_role_name).get('AttachedPolicies', [])
    if any(p['PolicyArn'] == eb_p_arn for p in attached):
        print(f"    (Already attached: {eb_p_name} to {eventbridge_role_name})")
    else:
        iam.attach_role_policy(RoleName=eventbridge_role_name, PolicyArn=eb_p_arn)

    # 7. EventBridge Scheduler (Schedules)
    eb_role_arn = f"arn:aws:iam::{account_id}:role/{eventbridge_role_name}"
    auto_check_expr = os.getenv('AUTO_CHECK_SCHEDULE', 'rate(5 minutes)')
    daily_stop_expr = os.getenv('DAILY_STOP_CRON', 'cron(0 15 * * ? *)')

    schedules = [
        {"Name": auto_check_name, "Expr": auto_check_expr, "Target": os.getenv('WORKER_LAMBDA_NAME'), "Input": '{"action": "auto-check"}'},
        {"Name": daily_stop_name, "Expr": daily_stop_expr, "Target": os.getenv('EXECUTOR_LAMBDA_NAME'), "Input": '{"action": "stop"}'}
    ]

    for sch in schedules:
        try:
            current = scheduler.get_schedule(Name=sch["Name"])
            # RoleArn が一致するか確認し、不一致なら更新
            if current.get('Target', {}).get('RoleArn') != eb_role_arn:
                print(f"🔄 Updating Schedule Role: {sch['Name']}...")
                scheduler.update_schedule(
                    Name=sch["Name"],
                    ScheduleExpression=sch["Expr"],
                    Target={
                        'Arn': f"arn:aws:lambda:{region}:{account_id}:function:{sch['Target']}",
                        'RoleArn': eb_role_arn,
                        'Input': sch['Input']
                    },
                    FlexibleTimeWindow={'Mode': 'OFF'}
                )
            else:
                print(f"    (Schedule {sch['Name']} already exists with correct role)")
        except ClientError:
            print(f"⏰ Creating Schedule: {sch['Name']}...")
            scheduler.create_schedule(
                Name=sch["Name"],
                ScheduleExpression=sch["Expr"],
                Target={
                    'Arn': f"arn:aws:lambda:{region}:{account_id}:function:{sch['Target']}",
                    'RoleArn': eb_role_arn,
                    'Input': sch['Input']
                },
                FlexibleTimeWindow={'Mode': 'OFF'}
            )

    # 8. EventBridge Rule (EC2 State)
    try:
        events.describe_rule(Name=rule_name)
        current_targets = events.list_targets_by_rule(Rule=rule_name).get('Targets', [])
        # ターゲットにRoleArnがセットされているか確認
        if not any(t.get('Id') == '1' and t.get('RoleArn') == eb_role_arn for t in current_targets):
            print(f"🔄 Updating Event Rule Target Role: {rule_name}...")
            events.put_targets(Rule=rule_name, Targets=[{
                'Id': '1', 
                'Arn': f"arn:aws:lambda:{region}:{account_id}:function:{os.getenv('EXECUTOR_LAMBDA_NAME')}",
                'RoleArn': eb_role_arn
            }])
        else:
            print(f"    (Event Rule {rule_name} already exists with correct role)")
    except ClientError:
        print(f"🔔 Setting up EC2 State-change Rule: {rule_name}...")
        pattern = {"source": ["aws.ec2"], "detail-type": ["EC2 Instance State-change Notification"], "detail": {"instance-id": [instance_id], "state": ["running", "stopped"]}}
        events.put_rule(Name=rule_name, EventPattern=json.dumps(pattern), State='ENABLED')
        events.put_targets(Rule=rule_name, Targets=[{
            'Id': '1', 
            'Arn': f"arn:aws:lambda:{region}:{account_id}:function:{os.getenv('EXECUTOR_LAMBDA_NAME')}",
            'RoleArn': eb_role_arn
        }])
        
        # Lambda Permission
        executor_name = os.getenv('EXECUTOR_LAMBDA_NAME')
        statement_id = f"AllowEventBridgeNotify-{env_arg}"
        rule_arn = f"arn:aws:events:{region}:{account_id}:rule/{rule_name}"

        add_needed = True
        try:
            policy_resp = awslambda.get_policy(FunctionName=executor_name)
            policy_dict = json.loads(policy_resp['Policy'])
            for statement in policy_dict.get('Statement', []):
                sid = statement.get('Sid')
                if sid in [statement_id, f"AlllowEventBridgeNotify-{env_arg}"]:
                    current_source_arn = statement.get('Condition', {}).get('ArnLike', {}).get('AWS:SourceArn')
                    if current_source_arn == rule_arn:
                        add_needed = False
                    else:
                        awslambda.remove_permission(FunctionName=executor_name, StatementId=sid)
                    break
        except ClientError:
            pass

        if add_needed:
            try:
                awslambda.add_permission(
                    FunctionName=executor_name,
                    StatementId=statement_id,
                    Action="lambda:InvokeFunction",
                    Principal="events.amazonaws.com",
                    SourceArn=rule_arn
                )
            except ClientError: pass
        print("✅ EventBridge rule and permission set.")

    print(f"\n✨ Infrastructure setup complete for [{env_arg.upper()}]!")

if __name__ == "__main__":
    # セッション確認
    try:
        if not boto3.Session().get_credentials() and not os.getenv('AWS_PROFILE'):
            # Load profile from .env if possible
            BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            env_arg = sys.argv[1] if len(sys.argv) > 1 else "prod"
            load_dotenv(os.path.join(BASE_DIR, f".env.{env_arg}" if env_arg != "prod" else ".env"))
        
        main()
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)