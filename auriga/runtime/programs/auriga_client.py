"""Dependency-free client for a preconnected, invocation-owned program channel."""
import base64
import io
import json
import os
import threading

class ActionIncompleteError(RuntimeError):
    """当 require_completed=True 且动作回执状态不是 completed 时抛出的异常。"""

    def __init__(self, response):
        data = response.get("data") if isinstance(response, dict) else None
        receipt = data.get("receipt") if isinstance(data, dict) else None
        self.response = response
        self.receipt = dict(receipt) if isinstance(receipt, dict) else {}
        self.plan_id = data.get("plan_id") if isinstance(data, dict) else None
        if isinstance(data, dict) and "next_action" in data and "next_action" not in self.receipt:
            self.receipt["next_action"] = data["next_action"]
        if self.plan_id and "plan_id" not in self.receipt:
            self.receipt["plan_id"] = self.plan_id
        status = self.receipt.get("status", "unknown")
        reason = self.receipt.get("reason")
        failed_stage = self.receipt.get("failed_stage_id")
        next_action = self.receipt.get("next_action") if isinstance(self.receipt.get("next_action"), dict) else {}
        start_from = next_action.get("start_from_stage_id")
        parts = [f"action_incomplete: status={status}"]
        if reason:
            parts.append(f"reason={reason}")
        if self.plan_id:
            parts.append(f"plan_id={self.plan_id}")
        if failed_stage:
            parts.append(f"failed_stage_id={failed_stage}")
        if start_from and start_from != failed_stage:
            parts.append(f"start_from_stage_id={start_from}")
        super().__init__(", ".join(parts))


class AurigaClient:
    def __init__(self):
        raw = os.fdopen(os.dup(int(os.environ["AURIGA_PROGRAM_FD"])),"r+b",buffering=0)
        self.pipe = io.BufferedRWPair(raw, raw)
        self.sequence=0
        self.lock=threading.RLock()
        self._unacknowledged_failed_action=None

    def acknowledge_failure(self):
        """显式确认已知悉上一次物理动作失败或空抓暂停，允许继续发起后续动作。"""
        self._unacknowledged_failed_action=None

    @staticmethod
    def ensure_completed(response):
        """断言动作响应已完成，否则抛出 ActionIncompleteError。"""
        receipt = ((response.get("data") or {}).get("receipt") or {}) if isinstance(response, dict) else {}
        if receipt.get("status") != "completed":
            raise ActionIncompleteError(response)
        return response

    def _request(self,value):
        with self.lock:
            self.pipe.write((json.dumps(value)+"\n").encode())
            self.pipe.flush()
            line=self.pipe.readline(32*1024*1024)
        if not line: raise RuntimeError("program_channel_closed")
        response=json.loads(line)
        if not response["ok"]: raise RuntimeError(response["error"])
        return response["result"]

    def call(self,tool_name,arguments=None):
        """Return success/kind/data plus display contentItems. For kind='action',
        read data['receipt']; success alone never establishes physical success.
        Old display-only replies have no typed evidence and remain unknown.
        """
        # A review may keep this RPC suspended while the same Python stack and
        # local variables are preserved. Only the main actor can resume it.
        args = dict(arguments or {})
        allow_after_failure = bool(args.pop("allow_after_failure", False))
        if allow_after_failure or tool_name in ("robot_observe", "geometry_query"):
            self._unacknowledged_failed_action = None
        if (
            tool_name == "robot_action"
            and (args.get("mode", "preview") in ("execute", "direct", "execute_if_clean") or args.get("auto_execute_if_clean"))
            and self._unacknowledged_failed_action is not None
        ):
            prev = self._unacknowledged_failed_action
            raise RuntimeError(
                f"program_blind_continuation_blocked: previous robot_action ({prev['action_id']}) "
                f"ended with status={prev['status']} reason={prev['reason']} "
                f"failed_stage_id={prev['failed_stage_id']} pending_stage_ids={prev['pending_stage_ids']}. "
                "Inspect the failure or call robot_observe before dispatching the next physical action."
            )
        with self.lock:
            self.sequence+=1
            result = self._request({"operation_id":str(self.sequence),"tool":tool_name,"arguments":args})
        if (
            tool_name == "robot_action"
            and (args.get("mode", "preview") in ("execute", "direct", "execute_if_clean") or args.get("auto_execute_if_clean"))
            and isinstance(result, dict)
            and result.get("kind") == "action"
        ):
            data = result.get("data")
            receipt = data.get("receipt") if isinstance(data, dict) else None
            if isinstance(receipt, dict) and receipt.get("physics_advanced") is not False:
                status = receipt.get("status")
                reasons = receipt.get("checkpoint_reasons") or {}
                empty_pause = (
                    status == "stopped"
                    and receipt.get("reason") == "observation_checkpoint"
                    and isinstance(reasons, dict)
                    and any("Empty grasp detected" in str(v) or "aperture_below_min_m" in str(v) for v in reasons.values())
                )
                if status == "failed" or empty_pause:
                    self._unacknowledged_failed_action = dict(
                        action_id=receipt.get("action_id"),
                        status=status,
                        reason=receipt.get("reason"),
                        failed_stage_id=receipt.get("failed_stage_id"),
                        pending_stage_ids=receipt.get("pending_stage_ids"),
                    )
                elif status == "completed":
                    self._unacknowledged_failed_action = None
        return result

    def action(self, stages=None, *, observation_id=None, require_completed=False, **arguments):
        """Request execution; the broker previews and yields for main-actor review
        unless that actor authorized direct actions when starting this program.
        When require_completed=True, raises ActionIncompleteError if receipt.status != 'completed'.
        """
        if isinstance(stages, dict):
            payload = dict(stages)
            payload.update(arguments)
            payload.setdefault('mode', 'direct')
            if observation_id is not None:
                payload['observation_id'] = observation_id
        else:
            if observation_id is None and arguments.get('mode', 'direct') != 'execute':
                raise TypeError("AurigaClient.action() missing 1 required keyword-only argument: 'observation_id'")
            payload = dict(arguments, mode=arguments.get('mode', 'direct'))
            if observation_id is not None:
                payload['observation_id'] = observation_id
            if stages is not None:
                payload['stages'] = stages
        result = self.call('robot_action', payload)
        if require_completed:
            self.ensure_completed(result)
        return result

    def artifact(self,reference):
        return base64.b64decode(self._request({"artifact_ref":reference})["data_base64"])

    def stereo_guide(self, *, observation_id, source_camera_id, target_camera_id, source_pixel):
        """只读双目极线约束查询辅助方法。"""
        return self.call("stereo_guide", dict(
            observation_id=observation_id,
            source_camera_id=source_camera_id,
            target_camera_id=target_camera_id,
            source_pixel=list(source_pixel),
        ))
