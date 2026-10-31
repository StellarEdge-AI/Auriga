"""Notify the user's existing Codex thread once when a benchmark queue exits."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import select
import subprocess
import uuid

from auriga.core import legacy_names as legacy
from auriga.cortex.session.pidfd import pidfd_open


TERMINAL = {"finished", "failed", "interrupted", "gpu_wait_timeout"}


def read_json(path):
    return json.loads(Path(path).read_text())


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    sync_directory(path.parent)


def process_identity(pid):
    try:
        # /proc comm may contain spaces and parentheses; starttime is field 22.
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(stat[19])
    except FileNotFoundError:
        return None


def wait_queue(output, pid, start_ticks):
    """Adopt a verified queue process and block in the kernel until its exit."""
    output = Path(output).resolve()
    if type(pid) is not int or pid <= 0 or type(start_ticks) is not int or start_ticks <= 0:
        raise ValueError("Adopted process requires positive integer pid and start_ticks")
    identity = process_identity(pid)
    if identity is None:
        queue_snapshot(output, process_exited=False)
        return
    if identity != start_ticks:
        raise RuntimeError("Queue PID was reused; identity differs")
    try:
        fd = pidfd_open(pid)
    except ProcessLookupError:
        queue_snapshot(output, process_exited=False)
        return
    try:
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")
        except FileNotFoundError:
            queue_snapshot(output, process_exited=False)
            return
        if not any(argv):
            queue_snapshot(output, process_exited=False)
            return
        command = argv[1:]
        while command and command[0] in {"-u", "-B", "-E", "-I", "-O", "-OO", "-P", "-s", "-S"}:
            command = command[1:]
        if not (command[:2] in [["-m", module] for module in ("auriga.apps.evaluation.queue", *legacy.QUEUE_MODULES)] or command
                and Path(command[0]).name in {"benchmark_queue.py", "auriga-queue", legacy.QUEUE_SCRIPT}):
            raise RuntimeError("Adopted process is not an Auriga benchmark queue")
        if "--output" not in argv or argv.index("--output") + 1 >= len(argv):
            raise RuntimeError("Adopted process has no queue output")
        if Path(argv[argv.index("--output") + 1]).resolve() != output:
            raise RuntimeError("Adopted process output differs from queue identity")
        if process_identity(pid) != identity:
            raise RuntimeError("Queue identity changed before wait")
        select.select([fd], [], [])
    finally:
        os.close(fd)


def queue_snapshot(output, process_exited, queue_stopped=False):
    try:
        state = read_json(output / "state.json")
        if not isinstance(state, dict):
            raise ValueError("state.json is not an object")
    except (OSError, ValueError) as error:
        if not process_exited:
            raise RuntimeError("Queue state missing or unreadable; completion unconfirmed") from error
        return {"status": "unknown", "state_error": str(error)}
    if not process_exited:
        if state.get("status") not in TERMINAL:
            raise RuntimeError("Queue has no terminal record; completion unconfirmed")
        cases = state.get("cases")
        if not isinstance(cases, list) or not cases:
            raise RuntimeError("Queue record has no cases; completion unconfirmed")
        if not queue_stopped and any(not isinstance(case, dict) or case.get("status") not in {"exited", "not_started"} for case in cases):
            raise RuntimeError("Queue contains a possibly active case; completion unconfirmed")
    return state


def prompt_for(output, state, process_exited, queue_stopped=False):
    cases = state.get("cases")
    incomplete = (state.get("status") not in TERMINAL or not isinstance(cases, list) or not cases
                  or any(not isinstance(case, dict) or case.get("status") not in {"exited", "not_started"} for case in cases))
    if process_exited and incomplete:
        return f"实验队列进程已退出，记录不完整、部分实验停止未确认，结果目录：{output}"
    if queue_stopped and (incomplete or state.get("cleanup_stop_unconfirmed")):
        return f"实验调度已结束，正常关闭未确认或发生强制回收，请检查清理记录，结果目录：{output}"
    return f"实验已结束，结果目录：{output}"


def notify_completion(output: Path, thread_id: str, codex="codex", *, process_exited=False, remote=None, queue_stopped=False):
    """Submit one notification; uncertain delivery is retained and never retried."""
    output = Path(output).resolve()
    thread_id = str(uuid.UUID(thread_id))
    remote = remote or "unix://" + str(Path.home() / ".codex/app-server-control/app-server-control.sock")
    claim = output / ".main-session-wake.json"
    artifacts = output / "completion-hook"

    def existing():
        receipt = read_json(claim)
        if receipt.get("thread_id") != thread_id:
            raise ValueError("Queue notification already claimed for a different thread")
        if (artifacts / "receipt.json").exists():
            receipt = read_json(artifacts / "receipt.json")
            if receipt.get("thread_id") != thread_id:
                raise ValueError("Notification receipt belongs to a different thread")
        return {**receipt, "already_claimed": True}

    if claim.exists():
        return existing()
    state = queue_snapshot(output, process_exited, queue_stopped)
    prompt = prompt_for(output, state, process_exited, queue_stopped)
    args = [str(codex), "queue", "--thread", thread_id, "--message", prompt, "--remote", remote]
    receipt = {"status": "delivery_unknown", "thread_id": thread_id, "queue_output": str(output),
               "claimed_at": datetime.now(timezone.utc).isoformat(), "artifacts": str(artifacts),
               "reason": "Claimed before submission; no automatic retry if interrupted"}
    try:
        stream = claim.open("x")
    except FileExistsError:
        return existing()
    with stream:
        json.dump(receipt, stream, indent=2, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(output)
    # Claim first: a crash or ambiguous CLI response must not duplicate a user turn.
    artifacts.mkdir(exist_ok=True)
    (artifacts / "prompt.txt").write_text(prompt)
    save_json(artifacts / "queue-state.json", state)
    save_json(artifacts / "command.json", {"argv": args})
    try:
        with (artifacts / "stdout.log").open("w") as stdout, (artifacts / "stderr.log").open("w") as stderr:
            result = subprocess.run(args, stdout=stdout, stderr=stderr, check=False)
        receipt.update(returncode=result.returncode,
                       status="sent" if result.returncode == 0 else "delivery_unknown",
                       reason="Codex queue accepted notification" if result.returncode == 0 else "Codex queue returned nonzero; delivery uncertain, no automatic retry")
    except OSError as error:
        receipt.update(returncode=None, reason=f"{type(error).__name__}: {error}; no automatic retry")
    receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
    save_json(artifacts / "receipt.json", receipt)
    save_json(claim, receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue-output", required=True, type=Path)
    parser.add_argument("--thread", required=True)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--remote", help="The main session app-server endpoint")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--start-ticks", type=int)
    args = parser.parse_args()
    if (args.pid is None) != (args.start_ticks is None):
        parser.error("--pid and --start-ticks must be supplied together")
    try:
        if args.pid is not None:
            wait_queue(args.queue_output, args.pid, args.start_ticks)
        receipt = notify_completion(args.queue_output, args.thread, args.codex, process_exited=args.pid is not None, remote=args.remote)
        print(json.dumps(receipt, ensure_ascii=False))
        return 0 if receipt["status"] == "sent" else 1
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f"{type(error).__name__}: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
