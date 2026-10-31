"""Startup, turn and shutdown waits without HTTP, Codex or simulator imports."""
import asyncio
import json
import time

INACTIVITY_TIMEOUT_S = 1800
INACTIVITY_REASON = "runtime_inactivity_timeout"


def model_budget_exhausted(session):
    event = getattr(session, "model_limit_reached", None)
    proxy = getattr(session, "model_proxy", None)
    # The flag is set before HTTP rejection; its queued Store callback may lag.
    return bool((event and event.is_set()) or (proxy and getattr(proxy, "limit_notified", False)))


def robot_gpu_oom(harness):
    failure = getattr(getattr(harness, "robot", None), "failure", None)
    return isinstance(failure, str) and failure.startswith("robot_gpu_out_of_memory:")


async def start_with_shutdown(workbench, shutdown, **arguments):
    """Stop admission on shutdown while retaining ownership of startup threads."""
    starting = asyncio.create_task(workbench.start(**arguments))
    stopping = asyncio.create_task(shutdown.wait())
    deadline = asyncio.create_task(asyncio.sleep(900))
    try:
        await asyncio.wait((starting, stopping, deadline), return_when=asyncio.FIRST_COMPLETED)
        if deadline.done() and not starting.done():
            raise asyncio.TimeoutError("official_startup_timeout")
        if shutdown.is_set() and not starting.done():
            return  # The original startup owner is released in finally.
        # Cancelling a to_thread await does not cancel its native initialization.
        # Keep the original startup owner until it releases its resources or its
        # existing deadline fires; never begin an actor after admission closes.
        await asyncio.shield(starting)
    finally:
        stopping.cancel()
        deadline.cancel()
        await asyncio.gather(stopping, deadline, return_exceptions=True)
        if not starting.done():
            try:
                await workbench.stop("shutdown-during-startup" if shutdown.is_set() else "startup-owner-cleanup")
            finally:
                abort = getattr(workbench, "abort_startup", None)
                aborting = asyncio.create_task(abort(starting)) if abort else None
                try:
                    # Keep ownership while cancelling processes, including any
                    # created after shutdown was requested by an in-flight start.
                    await asyncio.shield(asyncio.gather(starting, return_exceptions=True))
                finally:
                    if aborting:
                        await asyncio.shield(aborting)


async def wait_turn(session, shutdown, budget_s=None, harness=None):
    # This is host supervision, not an actor tool or model polling loop.
    deadline = time.monotonic() + budget_s if budget_s is not None else None
    if harness is not None:
        harness.wall_deadline = deadline
        harness.wait_stop_reason = None
    started = getattr(harness, "started_at", time.monotonic())
    while not shutdown.is_set():
        now = time.monotonic()
        reason = None
        proxy = getattr(session, "model_proxy", None)
        if getattr(proxy, "transport_blocked", False) is True:
            if harness is not None:
                harness.wait_stop_reason = "model_transport_terminal_error"
            raise RuntimeError("model_transport_terminal_error:" + json.dumps(proxy.blocked_error))
        if harness is not None and harness.episode_done.is_set():
            reason = "native_episode_ended"
        elif robot_gpu_oom(harness):
            reason = "robot_gpu_out_of_memory"
        elif model_budget_exhausted(session):
            reason = "model_request_limit"
        elif deadline is not None and now >= deadline:
            reason = "development_wall_budget"
        elif harness is not None:
            activity = [started, getattr(harness, "last_native_progress_at", started)]
            for owner in (session, proxy, getattr(harness, "robot", None), getattr(harness, "programs", None)):
                stamp = getattr(owner, "last_activity_at", None)
                if type(stamp) in (int, float):
                    activity.append(stamp)
            if now - max(activity) >= INACTIVITY_TIMEOUT_S:
                reason = INACTIVITY_REASON
        if reason:
            if harness is not None:
                harness.wait_stop_reason = reason
            return False
        if session.turn_done.is_set():
            return True
        await asyncio.sleep(.05)
    return session.turn_done.is_set()
