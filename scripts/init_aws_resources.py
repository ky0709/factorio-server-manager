import os
import boto3
import sys
import json
import zipfile
import io
import time
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
    
    region = os.getenv('AWS_REGION', 'ap-northeast-1')
    account_id = os.getenv('AWS_ACCOUNT_ID')
    bucket_name = os.getenv('S3_BUCKET_NAME')
    table_name = os.getenv('DYNAMODB_TABLE_NAME')
    instance_id = os.getenv('INSTANCE_ID')

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
        'WORK_POLICY_NAME': os.getenv('WORK_POLICY_NAME')
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
    role_name = f"FactorioLambdaRole{suffix}"
    scheduler_role_name = f"FactorioSchedulerRole{suffix}"

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

    # 3. IAM Role (Lambda)
    try:
        iam.get_role(RoleName=role_name)
        print(f"    (IAM Role {role_name} already exists)")
    except ClientError:
        print(f"👤 Creating IAM Role for Lambda: {role_name}...")
        trust_policy = {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]
        }
        iam.create_role(RoleName=role_name, AssumeRolePolicyDocument=json.dumps(trust_policy))
        iam.attach_role_policy(RoleName=role_name, PolicyArn='arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole')
        print("✅ IAM Role for Lambda created.")

    # 4. Initial IAM Policies (Placeholders)
    policies = [
        os.getenv('INTERACT_POLICY_NAME'), os.getenv('EXECUTE_POLICY_NAME'),
        os.getenv('NOTIFY_POLICY_NAME'), os.getenv('SERVER_POLICY_NAME'),
        os.getenv('REGIST_POLICY_NAME'), os.getenv('WORK_POLICY_NAME')
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

    # 5. Lambda Placeholders
    functions = [
        os.getenv('INTERACTOR_LAMBDA_NAME'), os.getenv('EXECUTOR_LAMBDA_NAME'),
        os.getenv('WORKER_LAMBDA_NAME'), os.getenv('NOTIFIER_LAMBDA_NAME')
    ]
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w') as zf:
        zf.writestr('lambda_function.py', "def lambda_handler(event, context): return {'status': 'initializing'}")
    
    print("λ Checking Lambda functions...")
    for f_name in functions:
        if not f_name: continue
        try:
            awslambda.get_function(FunctionName=f_name)
            print(f"    (Already exists: {f_name})")
        except ClientError:
            print(f"  - Creating {f_name}...")
            awslambda.create_function(
                FunctionName=f_name,
                Runtime='python3.12',
                Role=f'arn:aws:iam::{account_id}:role/{role_name}',
                Handler='lambda_function.lambda_handler',
                Code={'ZipFile': zip_buffer.getvalue()}
            )

    # 6. Scheduler IAM Role
    try:
        iam.get_role(RoleName=scheduler_role_name)
        print(f"    (IAM Role {scheduler_role_name} already exists)")
    except ClientError:
        print(f"👤 Creating IAM Role for Scheduler: {scheduler_role_name}...")
        trust_policy = {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Principal": {"Service": "scheduler.amazonaws.com"}, "Action": "sts:AssumeRole"}]
        }
        iam.create_role(RoleName=scheduler_role_name, AssumeRolePolicyDocument=json.dumps(trust_policy))
        policy_doc = {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": f"arn:aws:lambda:{region}:{account_id}:function:Factorio_*"}]
        }
        iam.put_role_policy(RoleName=scheduler_role_name, PolicyName="FactorioSchedulerPolicy", PolicyDocument=json.dumps(policy_doc))
        print("✅ IAM Role for Scheduler created.")

    # 7. EventBridge Scheduler (Schedules)
    sch_role_arn = f"arn:aws:iam::{account_id}:role/{scheduler_role_name}"
    auto_check_expr = os.getenv('AUTO_CHECK_SCHEDULE', 'rate(5 minutes)')
    daily_stop_expr = os.getenv('DAILY_STOP_CRON', 'cron(0 15 * * ? *)')

    schedules = [
        {"Name": auto_check_name, "Expr": auto_check_expr, "Target": os.getenv('WORKER_LAMBDA_NAME'), "Input": '{"action": "auto-check"}'},
        {"Name": daily_stop_name, "Expr": daily_stop_expr, "Target": os.getenv('EXECUTOR_LAMBDA_NAME'), "Input": '{"action": "stop"}'}
    ]

    for sch in schedules:
        try:
            scheduler.get_schedule(Name=sch["Name"])
            print(f"    (Schedule {sch['Name']} already exists)")
        except ClientError:
            print(f"⏰ Creating Schedule: {sch['Name']}...")
            scheduler.create_schedule(
                Name=sch["Name"],
                ScheduleExpression=sch["Expr"],
                Target={
                    'Arn': f"arn:aws:lambda:{region}:{account_id}:function:{sch['Target']}",
                    'RoleArn': sch_role_arn,
                    'Input': sch['Input']
                },
                FlexibleTimeWindow={'Mode': 'OFF'}
            )

    # 8. EventBridge Rule (EC2 State)
    try:
        events.describe_rule(Name=rule_name)
        print(f"    (Event Rule {rule_name} already exists)")
    except ClientError:
        print(f"🔔 Setting up EC2 State-change Rule: {rule_name}...")
        pattern = {"source": ["aws.ec2"], "detail-type": ["EC2 Instance State-change Notification"], "detail": {"instance-id": [instance_id], "state": ["running", "stopped"]}}
        events.put_rule(Name=rule_name, EventPattern=json.dumps(pattern), State='ENABLED')
        events.put_targets(Rule=rule_name, Targets=[{'Id': '1', 'Arn': f"arn:aws:lambda:{region}:{account_id}:function:{os.getenv('EXECUTOR_LAMBDA_NAME')}"}])
        
        # Lambda Permission
        try:
            awslambda.add_permission(
                FunctionName=os.getenv('EXECUTOR_LAMBDA_NAME'),
                StatementId=f"AllowEventBridgeNotify-{env_arg}",
                Action="lambda:InvokeFunction",
                Principal="events.amazonaws.com",
                SourceArn=f"arn:aws:events:{region}:{account_id}:rule/{rule_name}"
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