"""Observer-only task rules and recorded scores; never import or run the simulator.

The installed task source describes rules, not the archived episode's per-check
outcomes. A version comparison and an explicit missing-breakdown state accompany
every review. Library and standalone human HTTP viewers share this service;
actor tools and prompts do not expose it.
"""
from __future__ import annotations

import ast
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import subprocess
import threading
import time

from .experiments import evidence_path, read_json
from ...cortex.supervision.actor_stall import ACTOR_STALL_REASON
from .scoring import load_effective_summary


# Human descriptions were checked against these exact installed task files.
# A changed task falls back to its freshly parsed source instead of retaining a
# translation that might describe a different predicate or threshold.
REVIEWED_TASK_HASHES = {
    "arrange_largest_number": "277acd20ae4345dc71267762e588b7b0343387c71685183890f044a4808b7a20",
    "arrange_largest_number_random": "01757509978663bf7c0c95059e8ae18d45b8fe0953f6b54540e2bd6bf141182e",
    "build_tower": "814cf6ffb459871e5cf49f4e127903dbfbc30926f33db71de0b84a71ab235d6e",
    "classify_objects": "aef4333c088766375e21a928d53a9a93aaee285fecae41e22385acdc74dd11ac",
    "classify_objects_by_language": "242a8f048f487eb174ef246be78613bbb5f153549ee11a2650412718cf3b36c9",
    "fold_clothes": "ecea9c287d0579605330168e6f36734704fb14a6b1e5dec82b6f2ee3a56ec20a",
    "fold_clothes_random": "9e40abe3f7201a8c13961edc1ca8075260c315584d3d6b93fa1df966d13d1e88",
    "imitate_sorting_sequence": "18400cb3c5f5103492819df2dcdf05a77916106e8a503cb62b4ee6cdbaa9305b",
    "make_kong": "b9266689d65f9213b05219ad952344f9fd63be8892097ec0103af99791b245d5",
    "organize_table": "9e3057e70b8ad0b8cb553be6dec00d2b71fada6f4087edcb38fc56508f572dbd",
    "pack_objects_into_box": "84a1cf1dd5e166f36291130f9d8cb5dfe4b3f4732a46674bef6374a7ea2d66be",
    "pack_objects_into_box_random": "4db259a1b97cdcc39ebc9794244fadc79d31c4ae64069c9b11a233356fcb655c",
    "put_bottles_into_dustbin": "3ba6e19df0c5ff19ba961092f031ce14e91e72a9a4a5934eece6000a1ffa1c48",
}
REVIEWED_MANAGER_HASH = "bc56efd3aaa861b4b38960dc4fb7778a9b4da9cf131f815ac3170300dd233a41"
REVIEWED_PARSER_HASH = "eb928a05c3eb6bae429a922c59e64272c2c3851c22d779143d45627e47b3a504"
RETURN_TO_ORIGIN = "所有被测机械臂回到初始末端位姿：每个位置坐标的偏差不超过 0.15 m，姿态角差不超过 20°。"
OPEN_GRIPPERS = "所有被测夹爪的实测归一化开度至少为 0.8。"
AGGREGATION = {
    "transition": "阶段分依次推进，每个仿真检查步最多前进一级；显示已到达阶段的分值，各档不相加。若过程已到 100 分但任务成功条件未通过，原生评分回退到前一档。",
    "by_count": "按已完成条件组的数量查评分阶梯，各档不相加；触发条件满足时才检查。到达 100 分但任务成功条件未通过时，原生评分回退到前一档。",
    "paired": "各条件组对应独立分值，已完成项相加；合计为 100 分但任务成功条件未通过时，原生评分不计最后一项。",
    "binary_success": "该任务未定义分段 get_score 过程分；以原生 run_reward 的成功判定为准。Auriga 保存的归一化分在成功时为 1，失败且无过程分时为 0。",
    "unknown": "无法从可用源码确定评分聚合方式；不根据任务名称或总分补写规则。",
}


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _stamp(path):
    try:
        if path.is_symlink():
            return ("symlink",)
        value = path.stat()
        return value.st_size, value.st_mtime_ns
    except FileNotFoundError:
        return None


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def repository_version(root):
    """Use the same stripped git-diff fingerprint as robot_worker._versions."""
    try:
        def git(*args):
            return subprocess.check_output(["git", "-C", str(root), *args], text=True,
                                           stderr=subprocess.DEVNULL, timeout=10).strip()
        return {"commit": git("rev-parse", "HEAD"), "diff_sha256": _sha(git("diff").encode())}
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "diff_sha256": None}


def source_root(config=None):
    if config is not None:
        return Path(config.backend.root).expanduser()
    from ...core.config import Config
    root = Path(Config.load().backend.root).expanduser()
    # Retain the standalone source viewer's historical case-sensitive discovery.
    alternate = Path.home() / "code/RoboDojo"
    if (root == Path.home() / "code/robodojo" and not (root / "task/RoboDojo/tasks").is_dir()
            and (alternate / "task/RoboDojo/tasks").is_dir()):
        return alternate
    return root


def _literal(node, assignments):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        if isinstance(node, ast.Name):
            return assignments.get(node.id)
        return None


def _method_map(tree):
    return {node.name: node for cls in tree.body if isinstance(cls, ast.ClassDef)
            for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _template(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(part.value if isinstance(part, ast.Constant) else "{" + ast.unparse(part.value) + "}"
                       for part in node.values)
    return None


def _instruction(method):
    if method is None:
        return []
    values = []
    for node in ast.walk(method):
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "templates" for target in node.targets):
            if isinstance(node.value, (ast.List, ast.Tuple)):
                values.extend(filter(None, (_template(part) for part in node.value.elts)))
    return list(dict.fromkeys(values))


def _human_criterion(task, index, variant):
    """Reviewed summaries of the installed predicates, with their real units."""
    base = task.removesuffix("_random")
    count = index + 1
    if base == "arrange_largest_number":
        return (f"{count} 个数字放在正确位置并朝向正确",
                f"按数字从大到小确定从左到右的目标垫位，任意 {count} 个数字同时通过对应检查。"
                "数字与目标垫的 XY 距离阈值为 0.02 m；局部 Y 轴与世界 +Y 的夹角阈值为 45°，"
                "数字 0 允许正反 Y 轴；除 0、8 外还检查局部 +X 与世界 +X 的夹角阈值 45°。" + OPEN_GRIPPERS)
    if base == "classify_objects":
        return (f"{count} 个篮筐完成按类别归置",
                f"任意 {count} 个篮筐分别完整收纳一个类别，不能混入其它类别。"
                "该类别每个物体的位置点高度必须低于篮筐包围盒最高点加 0.01 m；"
                "包含判定使用原生 is_all_A_in_B，不能仅凭画面中心重合判断。" + OPEN_GRIPPERS)
    if base == "classify_objects_by_language":
        return (f"{count} 类物品进入指令指定的篮筐",
                f"任意 {count} 类物品按本轮语言指令完整放入左、中、右对应篮筐，且没有其它类别混入。"
                "每个物体的位置点高度低于篮筐包围盒最高点加 0.01 m。" + OPEN_GRIPPERS)
    if base == "organize_table":
        return (f"{count} 项桌面整理条件完成",
                f"鼠标、键盘、摆件、闹钟中任意 {count} 项通过各自检查：各物体相邻检查的位置变化阈值为 0.002 m；"
                "鼠标在鼠标垫内且朝向阈值 45°；键盘包围盒覆盖 x=[0,0.2]、y=[-0.03,0.03] m 的框区并朝向正确；"
                "摆件直立、位于支座上方 0.005–0.1 m，XY 距离阈值随摆件类别为 0.02–0.045 m；"
                "闹钟直立、位于抽屉上方 0.225–0.3 m。" + OPEN_GRIPPERS)
    if base == "put_bottles_into_dustbin":
        return (f"{count} 个瓶子放入垃圾桶",
                f"四个瓶子中任意 {count} 个通过原生桶底支承区域检查；瓶子包围盒底部与桶底的高度间隔为 0–0.4 m。" + OPEN_GRIPPERS)
    if base == "pack_objects_into_box":
        return (f"{count} 件物品按规定方向装箱",
                f"玩具车、电动牙刷、锤子、鞋中任意 {count} 件通过盒底功能区域与朝向检查。"
                "物体轴与箱体长边在 XY 平面的夹角阈值为 45°，物体规定正面朝左的夹角阈值为 60°；"
                "箱体直立，箱体 ±Y 轴与世界 +X 的夹角阈值为 40°。" + OPEN_GRIPPERS)
    if base == "imitate_sorting_sequence":
        return (f"按演示顺序放好前 {count} 件物品",
                f"前 {count} 件目标物体已在 basket0，后续目标物体仍在篮外；五件示范物体仍在 basket1。"
                "阶段必须按演示顺序前进，不能用最终全部入篮替代顺序证据。" + OPEN_GRIPPERS)
    if base == "build_tower":
        labels = ["完成塔底支承结构", "完成中层支承结构", "完成整座塔并对齐"]
        descriptions = [
            "底板 block0 由规定的两根支柱支承且直立；底板相邻检查的位置变化阈值为 0.002 m。",
            "底层保持正确，上层板 block7 由两侧支柱支承；支承圆半径为 0.043 m，block7 位置变化阈值为 0.002 m，相关木块直立阈值为 5°。",
            "全部八个部件满足底层、中层、顶层支承与直立条件；顶层支承圆半径 0.023 m，顶层两组轴对齐阈值为 20°，直立阈值为 5°。",
        ]
        return labels[index], descriptions[index] + OPEN_GRIPPERS
    if base == "fold_clothes":
        sleeve = "左袖对右胸的 x/y 偏差范围均为 (-0.125,0.14) m；右袖对左胸的 x 范围 (-0.14,0.125) m、y 范围 (-0.125,0.14) m。"
        trigger = "仅在所有夹爪实测开度跨入 ≥0.7 的上升沿检查；按已完成条件组数量取分。"
        if index == 0:
            return "两侧袖子折入胸前", sleeve + trigger
        return ("袖子与衣服下摆均折好", sleeve + "左右下摆相对同侧肩点的 x 范围 (-0.12,0.12) m、y 范围 (-0.095,0.17) m；下摆连线与肩线夹角小于 45°。" + trigger)
    return None


def _completion(task):
    base = task.removesuffix("_random")
    if base == "arrange_largest_number":
        return ["全部数字按从大到小的顺序放在对应垫位，并通过上述距离与方向检查。", RETURN_TO_ORIGIN]
    if base == "classify_objects":
        return ["三个篮筐分别通过整类别入篮检查；三个类别还分别通过对应 basket0/1/2 的位置点高度检查（篮顶 +0.01 m）。", RETURN_TO_ORIGIN]
    if base == "classify_objects_by_language":
        return ["三个类别都进入本次语言指令指定的篮筐，并通过篮頂 +0.01 m 的位置点高度检查。", RETURN_TO_ORIGIN]
    if base in {"organize_table", "put_bottles_into_dustbin", "pack_objects_into_box", "build_tower"}:
        return ["全部物体或结构通过上述任务几何检查；完整成功条件与过程分登记是分别检查的。", RETURN_TO_ORIGIN]
    if base == "imitate_sorting_sequence":
        return ["五次入篮状态按演示顺序依次通过；示范物体留在另一个篮筐。",
                "示范辅助臂未回位时不得提前操作；不能在前序目标尚未入篮时释放并稳定放好后序目标（稳定位置阈值 0.004 m）。", RETURN_TO_ORIGIN]
    if base == "fold_clothes":
        return ["在所有夹爪开度 ≥0.7 且所有目标机械臂回位的上升沿，双袖与下摆的完整折叠检查通过。",
                "另有两臂分别回位时触发的禁止状态计数，目标次数必须为 0；完整布料点条件见官方 run_reward。", RETURN_TO_ORIGIN]
    if base == "make_kong":
        return ["先等待对手弃牌，将对应三张手牌的局部 +Z 轴转向上方（阈值 30°），其它九张手牌保持局部 +Y 轴朝上（阈值 7°）。",
                "mahjong9_0 先通过目标四元数 [0,0.707,0.707,0] 的 7° 姿态检查，再恢复规定方向并到 XY=[0.319,-0.15] m 附近（阈值 0.015 m），夹爪打开。",
                "辅助臂未回位时，目标臂不得提前离开起点；原生禁止状态计数目标为 0。此任务没有独立分段过程分。"]
    return ["成功条件由官方 run_reward 定义；各条件的确切参数及嵌套与/或关系见下方规则源码。"]


def _parse_task(path, config_path, *, reviewed_dependencies=False):
    raw = path.read_bytes()
    source = raw.decode()
    tree = ast.parse(source)
    methods = _method_map(tree)
    score_method = methods.get("get_score")
    reviewed = reviewed_dependencies and REVIEWED_TASK_HASHES.get(path.stem) == _sha(raw)
    origin = {"path": str(path), "line": (score_method or methods.get("run_reward") or tree).lineno if score_method or methods.get("run_reward") else 1, "sha256": _sha(raw)}
    criteria, modes = [], []
    assignments = {}
    if score_method:
        for node in ast.walk(score_method):
            if isinstance(node, ast.Assign):
                value = _literal(node.value, assignments)
                if value is not None:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            assignments[target.id] = value
        calls = [node for node in ast.walk(score_method) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr in {"score", "score_single_env", "trigger_score", "final_score"}]
        for call_index, call in enumerate(calls):
            offset = 1 if call.func.attr in {"score_single_env", "trigger_score"} else 0
            if len(call.args) < 2 + offset:
                continue
            score_arg, checks = call.args[1 + offset], call.args[offset]
            gradient = _literal(score_arg, assignments)
            variants = [(None, gradient)]
            if isinstance(score_arg, ast.Subscript) and isinstance(score_arg.value, ast.Name):
                table = assignments.get(score_arg.value.id)
                if isinstance(table, dict):
                    variants = [(f"{key} 个数字" if path.stem.startswith("arrange_largest_number") else str(key), value) for key, value in table.items()]
            mode = next((_literal(keyword.value, assignments) for keyword in call.keywords if keyword.arg == "score_mode"), "paired")
            modes.append(mode)
            for variant, scores in variants:
                if not isinstance(scores, (list, tuple)) or not scores or not all(_number(value) for value in scores):
                    continue
                for index, score in enumerate(scores):
                    condition = checks.elts[index] if isinstance(checks, (ast.List, ast.Tuple)) and len(checks.elts) == len(scores) else checks
                    text = ast.get_source_segment(source, condition) or ast.unparse(condition)
                    human = _human_criterion(path.stem, index, variant) if reviewed else None
                    label, description = human or (f"评分条件组 {index + 1}", f"官方 {mode} 评分登记中的第 {index + 1} 组条件；完整条件和参数见规则源码。")
                    criteria.append(dict(id=f"{call_index}:{variant or 'default'}:{index}", label=label, description=description,
                                         score=score, score_label=f"{score:g} / 100", variant=variant,
                                         recorded_result=None, evidence_status="not_recorded",
                                         condition_code=text, source={**origin, "line": condition.lineno}))
    mode = modes[0] if modes and len(set(modes)) == 1 else "unknown" if score_method else "binary_success"
    if not criteria:
        conditions = _completion(path.stem) if reviewed else ["满足官方 run_reward 中登记的完整成功条件。"]
        for index, condition in enumerate(conditions):
            criteria.append(dict(id=f"completion:{index}", label=f"成功条件 {index + 1}", description=condition,
                                 score=None, score_label="无独立过程分" if not score_method else "分值无法静态解析",
                                 variant=None, recorded_result=None, evidence_status="not_recorded", source=origin))
    source_code = []
    selected = [name for name in methods if name in {"get_score", "run_reward"} or "checks" in name or "score_options" in name]
    for name in selected:
        method = methods[name]
        source_code.append(dict(title=name, path=str(path), line=method.lineno, text=ast.get_source_segment(source, method)))
    files = [{**origin, "line": 1}]
    if config_path.is_file() and not config_path.is_symlink():
        files.append(dict(path=str(config_path), line=1, sha256=_sha(config_path.read_bytes())))
    aggregation = AGGREGATION.get(mode, AGGREGATION["unknown"]) if reviewed_dependencies else "评分值与模式按当前任务源码中的登记原样提取；评分管理器版本未核验为已审阅版本，聚合细节以其源码为准。"
    return dict(mode=mode, max_score=100, aggregation=aggregation, criteria=criteria,
                completion_conditions=_completion(path.stem) if reviewed else ["完整成功条件见官方 run_reward 源码。"],
                source_code=source_code, files=files, templates=_instruction(methods.get("gen_instruction")),
                description_reviewed=reviewed)


def _recorded_instruction(directory):
    from ...core.storage.store import open_read_store
    database = directory / "events.sqlite"
    if not database.is_file() or database.is_symlink():
        return None
    try:
        with open_read_store(directory) as store:
            return store.recorded_task_instruction()
    except (OSError, ValueError, sqlite3.Error):
        return None


def _evaluation(value):
    if not isinstance(value, dict):
        return {}
    if isinstance(value.get("result"), dict):
        return value["result"]
    if any(key in value for key in ("native_reward", "native_process_score", "task_success", "normalized_score")):
        return value
    return {}


def _recorded_result(summary, evaluation, run_status):
    """Never turn a partial evaluator snapshot into an official scored episode."""
    scored = summary.get("scored_episode")
    outcome = summary.get("primary_outcome")
    excluded = scored is False or isinstance(outcome, str) and outcome.startswith("unscored_") or (bool(summary.get("infrastructure_error")) and scored is not True)
    status = "unscored" if excluded else "scored" if scored is True or isinstance(outcome, str) and outcome.startswith("scored_") else "partial" if evaluation or any(key in summary for key in ("native_process_score", "normalized_score")) else "pending" if run_status in {"running", "starting", "idle", "waiting"} else "unavailable"
    normalized = summary.get("normalized_score")
    # Explicit nulls from summary are authoritative. Older evaluator-only runs
    # retain their snapshot as partial; they acquire no invented scored flag.
    if "normalized_score" not in summary and not excluded:
        normalized = evaluation.get("normalized_score")
    task_success = summary.get("task_success") if "task_success" in summary else evaluation.get("task_success")
    if excluded:
        normalized = None
        task_success = False if summary.get("task_outcome") == "failure" else None
    native = {key: summary.get(key) if summary.get(key) is not None else evaluation.get(key)
              for key in ("native_process_score", "native_reward")}
    partial = deepcopy(summary.get("partial_native_result"))
    if partial is None and status in {"unscored", "partial"} and evaluation:
        partial = {key: evaluation[key] for key in ("normalized_score", "native_process_score", "native_reward", "task_success", "valid_episode", "success_authority", "authority") if key in evaluation}
    explanation = []
    if isinstance(summary.get("failure_reason"), dict):
        explanation.append("任务失败原因：" + summary["failure_reason"]["label"] + "。任务成功状态与过程得分分别记录。")
    if summary.get("scoring_reconciliation", {}).get("applied"):
        explanation.append("此历史运行已用保存的原生评测核验，并按当前任务上限规则恢复失败和过程分；原始归档保留旧分类不改写。")
    if summary.get("success_authority") == "harness_task_limit":
        explanation.append("本次因任务执行上限结束，任务失败已记录；有效归一化分取停止时原生过程分，得分证据不足时不纳入计分样本，不根据局部评测的成功快照覆盖失败状态。")
    if summary.get("termination_reason") == ACTOR_STALL_REASON:
        if task_success is False:
            explanation.append("模型连续 5 次声明无法继续完成，harness 提前结束运行并判为任务失败；过程分取停止后的原生正式检查。")
        else:
            explanation.append("模型连续 5 次声明无法继续完成触发 harness 停止；最终任务结果尚未确认。")
    if excluded:
        explanation.append(f"本次运行未计入正式任务评分（{outcome or 'scored_episode=false'}）；没有可核验的正式归一化分数；任务结果与分数可用性分别记录。局部原生评测只作为当时状态的证据。")
        if summary.get("infrastructure_error"):
            explanation.append("基础设施错误：" + str(summary["infrastructure_error"]))
    elif status == "partial":
        explanation.append("归档保存了评测或分数快照，但没有明确记录本次是否属于正式计分样本；按局部记录展示。")
    elif status in {"pending", "unavailable"}:
        explanation.append("尚未保存可用的本次评测结果。")
    if _number(normalized):
        if task_success is True and normalized == 1:
            explanation.append("归档记录任务成功；Auriga 的成功分支将归一化分数记为 1。原生过程分另行保留，不替代成功判定。")
        elif _number(native["native_process_score"]) and math.isclose(normalized, native["native_process_score"] / 100, abs_tol=1e-9):
            explanation.append(f"归档归一化分 = 原生过程分 {native['native_process_score']:g} ÷ 100 = {normalized:g}。")
        elif task_success is False and native["native_process_score"] is None and normalized == 0:
            explanation.append("归档记录任务未成功且没有原生过程分；Auriga 将归一化分数记为 0。")
        else:
            explanation.append("按归档原值展示分数；现有字段不足以核对该值的计算分支，不重新计算或覆盖记录。")
    if _number(native["native_process_score"]):
        explanation.append(f"原生过程分记录为 {native['native_process_score']:g} / 100，来自 RoboDojo RewardManager；native_reward 记录为 {native['native_reward']!s}，与过程分是不同字段。")
    explanation.append("本次逐条件真假、阶段到达记录及逐项得分明细未记录，不能仅根据总分反推出每项是否完成。")
    return dict(status=status, scored_episode=scored, primary_outcome=outcome, normalized_score=normalized,
                task_outcome=summary.get("task_outcome"), score_status=summary.get("score_status"),
                failure_reason=summary.get("failure_reason"), scoring_policy=summary.get("scoring_policy"),
                scoring_reconciliation=summary.get("scoring_reconciliation"), native_evaluation_snapshot=summary.get("native_evaluation_snapshot"),
                termination_reason=summary.get("termination_reason"), termination_authority=summary.get("termination_authority"),
                task_success=task_success, **native, partial_native_result=partial, explanation=explanation,
                breakdown_status="not_recorded", success_authority=summary.get("success_authority") or evaluation.get("success_authority"),
                valid_episode=summary.get("valid_episode", evaluation.get("valid_episode")),
                evaluation_normalized_score=evaluation.get("normalized_score"), evaluation_id=evaluation.get("evaluation_id"))


class TaskReviewService:
    """Small source/stat caches shared by both observer pages, with no run writes."""
    def __init__(self, root=None, *, config=None):
        self.root = Path(root).expanduser() if root is not None else source_root(config)
        self._rules = {}
        self._reviews = {}
        self._version = None
        self._version_at = 0
        self._lock = threading.RLock()

    def _installed_version(self):
        if self._version is None or time.monotonic() - self._version_at > 60:
            self._version = repository_version(self.root)
            self._version_at = time.monotonic()
        return self._version

    def _rules_for(self, task):
        unavailable = dict(mode="unknown", max_score=100, aggregation=AGGREGATION["unknown"], criteria=[],
                           completion_conditions=[], source_code=[], files=[], templates=[], description_reviewed=False)
        if not isinstance(task, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", task):
            return unavailable, None
        path = self.root / "task/RoboDojo/tasks" / (task + ".py")
        config = self.root / "task/RoboDojo/config" / (task + ".yml")
        manager = self.root / "env/reward_manager/reward_manager.py"
        parser = self.root / "env/reward_manager/func_parser.py"
        signature = tuple(_stamp(value) for value in (path, config, manager, parser))
        if any(value.is_symlink() for value in (path, config, manager, parser)) or not path.is_file() or not config.is_file():
            return unavailable, signature
        cached = self._rules.get(task)
        if cached and cached[0] == signature:
            return deepcopy(cached[1]), signature
        try:
            reviewed_dependencies = manager.is_file() and parser.is_file() and _sha(manager.read_bytes()) == REVIEWED_MANAGER_HASH and _sha(parser.read_bytes()) == REVIEWED_PARSER_HASH
            rules = _parse_task(path, config, reviewed_dependencies=reviewed_dependencies)
            for file in (manager, parser):
                if file.is_file():
                    rules["files"].append(dict(path=str(file), line=1, sha256=_sha(file.read_bytes())))
            if manager.is_file():
                manager_text = manager.read_text()
                methods = _method_map(ast.parse(manager_text))
                for name in ("_score_for_state", "get_score"):
                    if name in methods:
                        value = methods[name]
                        rules["source_code"].append(dict(title=f"RewardManager.{name}", path=str(manager), line=value.lineno,
                                                         text=ast.get_source_segment(manager_text, value)))
        except (OSError, UnicodeError, SyntaxError, ValueError, TypeError, IndexError):
            return unavailable, signature
        self._rules[task] = signature, deepcopy(rules)
        # A file change invalidates the repository fingerprint before any match.
        if cached:
            self._version_at = 0
        return rules, signature

    def get(self, entry):
        with self._lock:
            return deepcopy(self._get(entry))

    def _get(self, entry):
        directory = evidence_path(entry)
        manifest_path = directory / "manifest.json"
        summary_path = entry["path"] / "summary.json"
        evaluation_path = directory / "evaluation.json"
        manifest = read_json(manifest_path, {})
        summary = load_effective_summary(entry["path"], directory,
            summary=read_json(summary_path) or entry["item"].get("summary", {}))
        evaluation_record = read_json(evaluation_path, {})
        evaluation = _evaluation(evaluation_record) or _evaluation(summary.get("evaluation"))
        task = manifest.get("task") or entry["item"].get("task")
        issues = []
        if evaluation.get("task") not in {None, task}:
            issues.append("评测记录的 task 与运行 task 不一致，未使用该评测值。")
            evaluation = {}
        rules, rules_stamp = self._rules_for(task)
        installed = self._installed_version() if rules["files"] else {"commit": None, "diff_sha256": None}
        signature = (str(directory), task, tuple(_stamp(path) for path in (manifest_path, summary_path, evaluation_path, directory / "events.sqlite", directory / "events.sqlite-wal")),
                     rules_stamp, json.dumps(installed, sort_keys=True), json.dumps(summary, sort_keys=True), entry["item"].get("status"))
        cache_key = str(entry["path"])
        cached = self._reviews.get(cache_key)
        if cached and cached[0] == signature:
            return cached[1]
        versions = evaluation.get("versions") or manifest.get("capabilities", {}).get("versions") or manifest.get("versions", {})
        recorded = dict(commit=versions.get("robodojo_commit"), diff_sha256=versions.get("robodojo_local_diff_sha256"))
        matched = all(recorded.get(key) is not None and recorded[key] == installed.get(key) for key in ("commit", "diff_sha256"))
        provenance_status = "matched" if rules["files"] and matched else "current_source_only" if rules["files"] else "unavailable"
        provenance_message = ("本机 RoboDojo 提交与工作区差异指纹均与归档记录一致；以下规则来自该版本源码。" if provenance_status == "matched" else
                              "无法确认本机源码与本次历史运行的完整版本一致；以下是当前安装版本的规则参考，不是历史逐项评分证据。" if rules["files"] else
                              "当前安装目录没有此任务的可用源码与配置，未补写评分规则。")
        templates = rules.pop("templates")
        instruction = _recorded_instruction(directory) or manifest.get("task_instruction")
        if instruction:
            description = dict(text=instruction, source_kind="recorded_observation", status="recorded")
        else:
            description = dict(text="\n".join(templates) or None, source_kind="installed_task_source" if templates else None,
                               status="template" if templates else "not_recorded")
        files = rules.pop("files")
        rules["rule_source"] = dict(status=provenance_status, message=provenance_message, recorded=recorded, installed=installed, files=files)
        result = _recorded_result(summary, evaluation, entry["item"].get("status"))
        result["explanation"] = issues + result["explanation"]
        sources = []
        for label, path in (("运行清单", manifest_path), ("运行结论", summary_path), ("原生评测记录", evaluation_path)):
            if not path.is_file() or path.is_symlink():
                continue
            value = dict(label=label, path=str(path))
            if path.parent == directory and path.name in {"summary.json", "evaluation.json"}:
                identifier = hashlib.sha256(path.name.encode()).hexdigest()[:32]
                value["url"] = f'/v1/experiments/{entry["item"]["id"]}/files/{identifier}'
            sources.append(value)
        value = dict(schema_version="1", task_id=task, description=description, scoring=rules, result=result, sources=sources,
                     audience="observer_only", read_only=True)
        if len(self._reviews) >= 512:
            self._reviews.pop(next(iter(self._reviews)))
        self._reviews[cache_key] = signature, value
        return value
