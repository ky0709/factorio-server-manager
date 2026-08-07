"""EventBridge: EC2 関連イベントの処理（同一 Lambda 内分割）。"""
import json
import os
import time

from executor_infrastructure import consume_event_marker


def _collect_managed_instance_ids(config, factorio_state_table):
    local_id = str(config.get("instance_id") or "").strip().strip("'\"").lower()
    active_id = ""
    stop_target_id = ""
    if factorio_state_table:
        try:
            active_res = factorio_state_table.get_item(Key={'ConfigKey': 'ActiveInstanceId'})
            active_id = str((active_res.get('Item') or {}).get('Value') or "").strip().strip("'\"").lower()
        except Exception as e:
            print(f"⚠️ Failed to read ActiveInstanceId for event routing: {e}")
        try:
            # ID:090: terminate 後もイベント照合できるよう停止対象を保持
            stop_res = factorio_state_table.get_item(Key={'ConfigKey': 'StopTargetInstanceId'})
            stop_target_id = str((stop_res.get('Item') or {}).get('Value') or "").strip().strip("'\"").lower()
        except Exception as e:
            print(f"⚠️ Failed to read StopTargetInstanceId for event routing: {e}")
    return [v for v in [local_id, active_id, stop_target_id] if v]


def handle_ec2_instance_state_event(event, config, factorio_state_table, notify):
    """
    aws.ec2 / EC2 Instance State-change Notification を処理する。
    対象外・未初期化・ID不一致の場合も辞書を返して EventBridge 呼び出しを閉じる。
    """
    detail = event.get('detail', {})
    instance_id = detail.get('instance-id') or detail.get('instanceId') or ''

    is_suppressed = False
    if factorio_state_table:
        res = factorio_state_table.get_item(Key={'ConfigKey': 'TestSessionActive'})
        if 'Item' in res:
            is_suppressed = True

    raw_state = detail.get('state')
    if isinstance(raw_state, dict):
        state_name = raw_state.get('name')
    else:
        state_name = raw_state

    print(f"DEBUG: Received EC2 event for {instance_id} state={state_name}. Expected ID={config.get('instance_id')}")

    valid_ids = _collect_managed_instance_ids(config, factorio_state_table)
    remote_id = str(instance_id or "").strip().strip("'\"").lower()

    if not valid_ids:
        print(
            f"⚠️ Event ignored: both local_id(instance_id) and ActiveInstanceId are empty. "
            f"SSM path: {os.getenv('SSM_PARAMETER_PATH', '/factorio/')} (Config keys: {list(config.keys())})"
        )
        return {"status": "event_handled_or_ignored"}
    if not factorio_state_table:
        print("❌ Event ignored: factorio_state_table is not initialized. Skipping DB operations.")
        return {"status": "event_handled_or_ignored"}
    if remote_id not in valid_ids:
        print(f"ℹ️ Event ignored: ID mismatch. Remote={remote_id}, LocalCandidates={valid_ids}")
        return {"status": "event_handled_or_ignored"}

    try:
        # ID:090: EPHEMERAL は terminate されるため terminated も停止完了として扱う
        if state_name in ('stopped', 'terminated'):
            marker_acquired, start_time_str = consume_event_marker(factorio_state_table, 'StopStartTime')
            if not marker_acquired:
                print(f"ℹ️ Duplicate {state_name} event ignored (StopStartTime already consumed).")
                return {"status": "duplicate_ignored", "state": state_name}

            # 停止完了後に停止対象/アクティブ ID を掃除
            try:
                factorio_state_table.delete_item(Key={'ConfigKey': 'StopTargetInstanceId'})
            except Exception as e:
                print(f"⚠️ Failed to clear StopTargetInstanceId: {e}")
            try:
                factorio_state_table.delete_item(Key={'ConfigKey': 'ActiveInstanceId'})
            except Exception as e:
                print(f"⚠️ Failed to clear ActiveInstanceId after shutdown event: {e}")

            elapsed_msg = ""
            if start_time_str:
                elapsed = int(time.time() - float(start_time_str))
                elapsed_msg = f" (Total sequence time: {elapsed}s)"
            verb = "has terminated" if state_name == "terminated" else "has stopped"
            msg = f"🔌 [LOG] EC2 Instance ({instance_id}) {verb}. Shutdown sequence completed.{elapsed_msg}"
            if is_suppressed:
                print(f"🔕 [SILENT MODE] Suppressed Notification: {msg}")
            else:
                notify(msg, mode='log')
            return {"status": "ok", "suppressed_msg": msg if is_suppressed else None}
        if state_name == 'running':
            marker_acquired, start_time_str = consume_event_marker(factorio_state_table, 'StartStartTime')
            if not marker_acquired:
                print("ℹ️ Duplicate running event ignored (StartStartTime already consumed).")
                return {"status": "duplicate_ignored", "state": "running"}

            elapsed_msg = ""
            if start_time_str:
                elapsed = int(time.time() - float(start_time_str))
                elapsed_msg = f" (EC2 boot time: {elapsed}s)"
            msg = f"🚀 [LOG] EC2 Instance ({instance_id}) is now running.{elapsed_msg}"
            if is_suppressed:
                print(f"🔕 [SILENT MODE] Suppressed Notification: {msg}")
            else:
                notify(msg, mode='log')
            return {"status": "ok", "suppressed_msg": msg if is_suppressed else None}
    except Exception as e:
        print(f"❌ Error processing EC2 state change: {e}")

    return {"status": "event_handled_or_ignored"}


def _notify_game_players_spot_warning(config, instance_id, get_client, run_rcon_command):
    """RCON でプレイヤーに中断予告を送る（ベストエフォート）。"""
    try:
        ec2 = get_client('ec2')
        inst = ec2.describe_instances(InstanceIds=[instance_id])['Reservations'][0]['Instances'][0]
        state_name = inst.get('State', {}).get('Name')
        ip = inst.get('PublicIpAddress')
        if state_name != 'running' or not ip:
            return False, f"instance state={state_name}, ip={ip}"

        # Factorio サーバー内に 1 分後停止を通知
        warn_cmd = "/server-message [OPS] Spot interruption notice received. Server will stop in about 1 minute. Please prepare to disconnect."
        res = run_rcon_command(ip, config['rcon_port'], config['rcon_password'], warn_cmd)
        if "Error" in str(res):
            return False, str(res)
        return True, str(res)
    except Exception as e:
        return False, str(e)


def _invoke_executor_stop(config, locale, get_client):
    """Executor に stop を非同期委譲する。"""
    try:
        payload = json.dumps({'action': 'stop', 'locale': locale})
        get_client('lambda').invoke(
            FunctionName=config['executor_lambda_name'],
            InvocationType='Event',
            Payload=payload
        )
        return True, None
    except Exception as e:
        return False, str(e)


def handle_managed_capacity_signal(event, config, factorio_state_table, notify, get_client, run_rcon_command):
    """Spot 中断警告 / Rebalance recommendation を受信し、保護動作を実行する。"""
    detail_type = event.get('detail-type') or ''
    detail = event.get('detail', {}) or {}
    instance_id = detail.get('instance-id') or detail.get('instanceId') or ''
    action = detail.get('action', '')

    if not factorio_state_table:
        print("❌ Capacity signal ignored: factorio_state_table is not initialized.")
        return {"status": "event_handled_or_ignored"}

    valid_ids = _collect_managed_instance_ids(config, factorio_state_table)
    remote_id = str(instance_id or "").strip().strip("'\"").lower()
    if not valid_ids or remote_id not in valid_ids:
        print(f"ℹ️ Capacity signal ignored: instance={remote_id} detail-type={detail_type} candidates={valid_ids}")
        return {"status": "event_handled_or_ignored"}

    is_suppressed = False
    try:
        res = factorio_state_table.get_item(Key={'ConfigKey': 'TestSessionActive'})
        if 'Item' in res:
            is_suppressed = True
    except Exception as e:
        print(f"⚠️ Failed to check TestSessionActive for capacity signal: {e}")

    msg = f"⚠️ [LOG] {detail_type}: instance={instance_id} action={action}"
    if is_suppressed:
        print(f"🔕 [SILENT MODE] Suppressed Notification: {msg}")
    else:
        notify(msg, mode='log')

    try:
        factorio_state_table.put_item(
            Item={
                'ConfigKey': 'SpotInterruptionNotice',
                'Timestamp': str(int(time.time())),
                'InstanceId': str(instance_id),
                'DetailType': str(detail_type),
                'Action': str(action),
            }
        )
    except Exception as e:
        print(f"⚠️ Failed to persist SpotInterruptionNotice: {e}")

    # Spot interruption warning では 1 分猶予後に停止シーケンスを実行する。
    if detail_type == 'EC2 Spot Instance Interruption Warning':
        locale = event.get('locale', 'ja')
        game_warn_ok, game_warn_detail = _notify_game_players_spot_warning(
            config,
            instance_id,
            get_client,
            run_rcon_command
        )
        game_warn_msg = (
            "⚠️ [LOG] Spot interruption warning received. "
            f"Sent in-game 1-minute warning to players. instance={instance_id}"
            if game_warn_ok
            else "⚠️ [LOG] Spot interruption warning received but failed to send in-game warning. "
                 f"instance={instance_id} reason={game_warn_detail}"
        )
        if is_suppressed:
            print(f"🔕 [SILENT MODE] Suppressed Notification: {game_warn_msg}")
        else:
            notify(game_warn_msg, mode='log')

        # Discord 利用者へ 1 分後停止の案内
        user_warn_msg = (
            "⚠️ Spot interruption notice received from AWS. "
            "The server will start shutdown operations in about 1 minute to protect save data."
        )
        if is_suppressed:
            print(f"🔕 [SILENT MODE] Suppressed Notification: {user_warn_msg}")
        else:
            notify(user_warn_msg, mode='webhook')

        # 要件: 通知から 1 分後に終了処理を開始
        wait_seconds = int(config.get('spot_interruption_grace_seconds', 60))
        time.sleep(max(0, wait_seconds))

        stop_ok, stop_err = _invoke_executor_stop(config, locale, get_client)
        stop_msg = (
            "🛑 Spot interruption handling: shutdown sequence executed. "
            "If you want to continue playing, please run /start again."
            if stop_ok
            else f"⚠️ Spot interruption handling failed to invoke /stop. reason={stop_err}"
        )
        if is_suppressed:
            print(f"🔕 [SILENT MODE] Suppressed Notification: {stop_msg}")
        else:
            # 利用者向け案内も含めて通常通知
            notify(stop_msg, mode='webhook')

    return {"status": "ok", "suppressed_msg": msg if is_suppressed else None}
