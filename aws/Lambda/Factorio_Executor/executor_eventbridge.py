"""EventBridge: EC2 Instance State-change Notification の処理（同一 Lambda 内分割）。"""
import os
import time

from executor_infrastructure import consume_event_marker


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

    local_id = str(config.get("instance_id") or "").strip().strip("'\"").lower()
    remote_id = str(instance_id or "").strip().strip("'\"").lower()

    if not local_id:
        print(
            f"⚠️ Event ignored: local_id (instance_id) is empty. "
            f"SSM path: {os.getenv('SSM_PARAMETER_PATH', '/factorio/')} (Config keys: {list(config.keys())})"
        )
        return {"status": "event_handled_or_ignored"}
    if not factorio_state_table:
        print("❌ Event ignored: factorio_state_table is not initialized. Skipping DB operations.")
        return {"status": "event_handled_or_ignored"}
    if remote_id != local_id:
        print(f"ℹ️ Event ignored: ID mismatch. Remote={remote_id}, Local={local_id}")
        return {"status": "event_handled_or_ignored"}

    try:
        if state_name == 'stopped':
            marker_acquired, start_time_str = consume_event_marker(factorio_state_table, 'StopStartTime')
            if not marker_acquired:
                print("ℹ️ Duplicate stopped event ignored (StopStartTime already consumed).")
                return {"status": "duplicate_ignored", "state": "stopped"}

            elapsed_msg = ""
            if start_time_str:
                elapsed = int(time.time() - float(start_time_str))
                elapsed_msg = f" (Total sequence time: {elapsed}s)"
            msg = f"🔌 [LOG] EC2 Instance ({instance_id}) has stopped. Shutdown sequence completed.{elapsed_msg}"
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
