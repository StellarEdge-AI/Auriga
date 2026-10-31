"""features.pose_skill (off by default): a read-only pose-planning tool, pose_plan, for the actor.

The tool computes geometry; it never moves the robot, never edits an action and never stores anything the
model does not see. With the switch off the tool is absent from the catalogue and the developer instructions
are unchanged.

Kinds (request.kind):

* targets: the actor reports, after reading the task's success criteria, whether final object orientation
  matters and, if it does, the absolute end rotation of every object it will move (long-axis yaw in world
  degrees, upright/lying, optional pitch/roll). The tool validates and normalises the values, records them,
  and returns one attention line for task_plan so the decision-boundary reminder carries it. Positions are not
  part of this kind.
* grasp: from one object-table row (by id from the table attached to the latest observation, or copied
  values) the tool returns geometry facts only: the row values, jaws closing across the long axis
  (angles_deg yaw = object yaw +/- 90, the candidate nearer the arm's measured yaw), the opening check against
  the 88.8 mm maximum, the pad height reference (centre z, raised only when the fingertips would reach the
  support) and a one-line self_prompt. The grasp point is left to the actor (a tool's handle, not its centre);
  an optional grasp_point_m + reason is placed relative to the long axis and footprint and logged. The reply
  is an ordinary tool output, so it stays in the following requests (appended, prefix unchanged).
* place: converts an object end yaw into the gripper yaw at release, from either the object's current table
  yaw (held object visible) or its yaw at grasp plus the gripper yaw at grasp.

Angle conventions (auriga/robot/model/roboprobe_pose.py): angles_deg = [pitch, roll, yaw]; [0, 0, 0] is top-down with
the jaws opening and closing along world x, and yaw rotates that closing line about world +z, so the closing
line of a top-down grasp lies at yaw (mod 180). object_table yaw_deg is the long-axis yaw in [-90, 90) from
world +x, so closing across the long axis is yaw_deg +/- 90.

Events (internal): pose_skill.targets, pose_skill.grasp, pose_skill.place.
"""
from __future__ import annotations

import json
import math

TOOL_NAME = "pose_plan"
MAX_OPENING_M = 0.0888          # ARX X5 maximum jaw opening
FINGERTIP_BELOW_PAD_M = 0.0075  # half the 15 mm gripping face: fingertips reach this far below pad_center
SUPPORT_CLEARANCE_M = 0.002
ELONGATED_RATIO = 1.5           # length/width at which the closing direction decides the grasp (cheap plan 5.53)
TABLE_COLUMNS = ("id", "centre_m", "top_z", "bottom_z", "length", "width", "yaw_deg", "tilt_deg", "support",
                 "quality")
POSTURES = ("lying", "upright", "any")
EVENT_TARGETS, EVENT_GRASP, EVENT_PLACE = "pose_skill.targets", "pose_skill.grasp", "pose_skill.place"


def enabled(runtime) -> bool:
    """features.pose_skill of the runtime's config; False without a config."""
    cfg = getattr(runtime, "config", runtime)
    features = getattr(cfg, "features", cfg)
    return bool(getattr(features, "pose_skill", False))


# ------------------------------------------------------------------ public text
DESCRIPTION = (
    "Read-only pose planning from the object table; no motion, no action edits. kind='targets': record whether final "
    "object orientation matters for this task and, if so, each moved object's absolute end rotation (long-axis yaw "
    "in world degrees, upright or lying, optional pitch/roll); returns attention_text to keep in task_plan attention. "
    "kind='grasp': for one object-table row (object id from the table attached to the latest observation, or copied "
    "row values) returns its geometry facts (centre, top, bottom, length, width, yaw, tilt), angles_deg with the jaws "
    "closing across the long axis (object yaw +/- 90 deg, the candidate nearer the arm's measured yaw), the width "
    "check against the 88.8 mm maximum opening, the pad height reference (centre z) and a one-line self_prompt; the "
    "grasp point itself is your choice (an optional grasp_point_m + reason is placed relative to the footprint). "
    "kind='place': converts an object end yaw into "
    "the gripper yaw at release. All values derive from VGGT estimates (not measurements); check them against "
    "current RGB. Angles use robot_action's angles_deg=[pitch,roll,yaw] convention: [0,0,0] is top-down with the "
    "jaws closing along world x; yaw rotates that line about world +z.")

_NUMBER3 = dict(type="array", items=dict(type="number"), minItems=3, maxItems=3)
ROW_SCHEMA = dict(type="object", additionalProperties=False,
    description="Copied object-table values when no attached-table id applies (e.g. an on-demand geometry_query table).",
    properties=dict(centre_m=dict(_NUMBER3), top_z=dict(type="number"), bottom_z=dict(type="number"),
                    length=dict(type="number", minimum=0), width=dict(type="number", minimum=0),
                    yaw_deg=dict(type="number"), tilt_deg=dict(type=["number", "null"])),
    required=["centre_m", "length", "width", "yaw_deg"])
TARGET_SCHEMA = dict(type="object", additionalProperties=False,
    properties=dict(
        object=dict(type="string", description="Object-table id at planning time and/or a short visible label."),
        destination=dict(type="string", description="Short description of where it goes (no coordinates needed)."),
        yaw_deg=dict(type=["number", "null"], description="Required end yaw of the object's long axis in world "
                     "degrees (any value; normalised to [-90, 90) because the long axis is symmetric); null when "
                     "yaw does not matter."),
        posture=dict(type="string", enum=list(POSTURES), description="lying = long axis horizontal (tilt ~90 deg "
                     "from vertical), upright = long axis vertical, any = not constrained."),
        pitch_deg=dict(type="number"), roll_deg=dict(type="number"),
        tolerance_deg=dict(type="number", minimum=0, maximum=90, description="Allowed yaw deviation; default 10."),
        basis=dict(type="string", description="Evidence: success criterion, fixture orientation, table row.")),
    required=["object", "posture"])
REQUEST_SCHEMA = dict(type="object", additionalProperties=False, required=["kind"], properties=dict(
    kind=dict(type="string", enum=["targets", "grasp", "place"]),
    needed=dict(type="boolean", description="targets: whether the success criteria constrain final orientation."),
    reason=dict(type="string", description="targets: one-line reason for the needed decision; grasp: why this "
                "grasp point (e.g. handle, rim, centre)."),
    grasp_point_m=dict(type="array", items=dict(type="number"), minItems=2, maxItems=3,
                       description="grasp: optional grasp point you chose (xy or xyz, env_origin m); the reply places it "
                       "relative to the object's long axis and footprint."),
    targets=dict(type="array", maxItems=12, items=TARGET_SCHEMA, description="targets: one entry per moved object."),
    object=dict(type="string", description="grasp: object-table id (oN) of the table attached to observation_id; "
                "place: id or label used in targets."),
    observation_id=dict(type="string", description="grasp: observation whose attached table holds object; default "
                        "the latest observation."),
    row=ROW_SCHEMA,
    arm=dict(type="string", enum=["left", "right"], description="grasp/place: arm whose measured yaw selects the "
             "nearer of the two equivalent jaw yaws."),
    target_yaw_deg=dict(type="number", description="place: required object long-axis end yaw (world deg); optional when target_posture='upright'."),
    source_posture=dict(type="string", enum=["lying", "upright"],
                        description="place: object posture at grasp ('lying' horizontal or 'upright' vertical); pair with target_posture for 3D release_angles_deg."),
    target_posture=dict(type="string", enum=["lying", "upright"],
                        description="place: required object posture at release ('lying' or 'upright'); lying <-> upright computes full 3D release_angles_deg=[pitch,roll,yaw] via a 90 deg rotation about the jaw closing line."),
    object_yaw_now_deg=dict(type="number", description="place: held object's long-axis yaw in the latest table."),
    object_yaw_at_grasp_deg=dict(type="number", description="place: object long-axis yaw when it was grasped."),
    gripper_yaw_at_grasp_deg=dict(type="number", description="place: angles_deg yaw used for that grasp; default "
                                  "the yaw of the last pose_plan grasp for this object.")))


def definition():
    """Public tool definition of pose_plan (the input field guide is added by tools.definitions)."""
    return dict(type="function", name=TOOL_NAME, description=DESCRIPTION,
                inputSchema=dict(type="object", additionalProperties=False, required=["request"],
                                 properties=dict(request=REQUEST_SCHEMA)))


INSTRUCTIONS = """
## Pose planning (pose_plan)
Read the task's success criteria to check whether the final orientation of moved objects matters (insertion,
hanging, placing onto a fixture or slot, aligning, standing upright or lying flat). When final orientation matters,
estimate each moved object's end rotation (long-axis yaw in world degrees, upright or lying, pitch/roll) from the
attached object table and destination fixture; you may record them with
pose_plan(request={kind:'targets', needed:true, targets:[...]}) and keep the returned attention_text in a task_plan
attention entry so the final waypoint reaches this rotation (for pure transport where orientation is free, either
skip pose_plan or call kind:'targets' with needed:false).
Notice that the attached object_table already provides inline `grasp_hints` (`across_yaw_deg = yaw_deg +/- 90`,
`pad_z_m`, `opening_check`) for every object with zero extra tool turns, and `robot_action(mode='preview')`
automatically audits jaw-to-long-axis alignment, grasp Z height, and grip_guard width in `grasp_3d_review`.
You can use `grasp_hints` directly in `robot_action`, or call pose_plan(request={kind:'grasp', object:'oN', arm,
grasp_point_m, reason}) on demand when you want to verify a specific off-centre grasp point (such as a tool's
handle or rim) or select the candidate nearer the arm's measured yaw. Jaws closing along the long axis of an
elongated object rarely lift it. Before releasing an object that requires a target yaw or a 3D posture transition
(such as standing a lying tube/pen upright via source_posture:'lying', target_posture:'upright'), call
pose_plan(request={kind:'place', ...}) for the release yaw and full 3D release_angles_deg=[pitch,roll,yaw], and
lower close to the support before opening.
"""


# ------------------------------------------------------------------ geometry
def wrap180(angle):
    """Angle in [-180, 180)."""
    return (float(angle) + 180.0) % 360.0 - 180.0


def wrap90(angle):
    """Axis angle (period 180) in [-90, 90)."""
    return (float(angle) + 90.0) % 180.0 - 90.0


def axis_difference(a, b):
    """Unsigned angle between two undirected horizontal axes, in [0, 90]."""
    return abs(wrap90(float(a) - float(b)))


def closure_yaws(object_yaw_deg):
    """The two angles_deg yaws whose jaw closing line is perpendicular to the long axis (same line, opposite wrist)."""
    return [round(wrap180(object_yaw_deg + 90.0), 1), round(wrap180(object_yaw_deg - 90.0), 1)]


def nearest(candidates, reference):
    """Candidate yaw closest to reference (smallest |yaw| without a reference); ties go to the smaller |yaw|."""
    if reference is None:
        return min(candidates, key=lambda value: (abs(value), value))
    return min(candidates, key=lambda value: (abs(wrap180(value - reference)), abs(value)))


def _num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def row_from_table(table, object_id):
    """Dict of one row of an attached table (columns as in object_table.COLUMNS); None when absent."""
    if not isinstance(table, dict):
        return None
    columns = list(table.get("columns") or [])
    for row in table.get("rows") or []:
        if isinstance(row, (list, tuple)) and row and row[0] == object_id:
            return dict(zip(columns, row))
    return None


def grasp_facts(row, *, measured_yaw=None, support_z=None, object_id=None, grasp_point=None, reason=None):
    """Geometry facts for a grasp of one table row; the grasp point stays the caller's choice. Pure function."""
    centre = [float(v) for v in row["centre_m"]]
    length, width, yaw = float(row["length"]), float(row["width"]), float(row["yaw_deg"])
    top, bottom, tilt = row.get("top_z"), row.get("bottom_z"), row.get("tilt_deg")
    ratio = length / width if width > 0 else None
    elongated = ratio is not None and ratio >= ELONGATED_RATIO
    upright = _num(tilt) and tilt < 30.0
    candidates = closure_yaws(yaw)
    chosen = nearest(candidates, measured_yaw)
    other = [c for c in candidates if c != chosen][0]
    support = bottom if _num(bottom) else support_z
    pad_z = centre[2]
    floor = float(support) + FINGERTIP_BELOW_PAD_M + SUPPORT_CLEARANCE_M if _num(support) else None
    z_raised = floor is not None and pad_z < floor
    if z_raised:
        pad_z = floor
    notes = []
    if width > MAX_OPENING_M:  # length >= width, so no top-down closing direction spans the whole footprint
        fit = "too_wide"
        notes.append("visible width %.0f mm exceeds the 88.8 mm opening; only a narrower part (rim, handle, edge) "
                     "fits between the jaws" % (width * 1000))
    elif width > MAX_OPENING_M - 0.010:
        fit = "tight"
        notes.append("visible width %.0f mm leaves under 10 mm of the 88.8 mm opening" % (width * 1000))
    else:
        fit = "ok"
    if not elongated:
        notes.append("compact footprint (length/width %s): closing direction matters little" %
                     ("%.2f" % ratio if ratio else "unknown"))
    if upright:
        notes.append("principal axis near vertical (tilt %.0f deg): yaw describes the cross-section only" % tilt)
    if _num(top) and top - centre[2] > 0:
        notes.append("centre z is %.0f mm below the visible top (VGGT tops of small curved objects read 1-3.5 cm "
                     "low; centre z is the better pad reference)" % ((top - centre[2]) * 1000))
    if z_raised:
        notes.append("pad reference raised to %.3f so the fingertips (7.5 mm below pad_center) stay %.0f mm above "
                     "the support" % (pad_z, SUPPORT_CLEARANCE_M * 1000))
    quality = row.get("quality")
    if isinstance(quality, str) and quality.startswith("low"):
        notes.append("table quality %s: verify position in RGB first" % quality)
    facts = dict(centre_m=[round(v, 4) for v in centre], top_z=round(float(top), 4) if _num(top) else None,
                 bottom_z=round(float(bottom), 4) if _num(bottom) else None, length=length, width=width,
                 yaw_deg=yaw, tilt_deg=tilt if _num(tilt) else None, quality=quality)
    jaws = dict(angles_deg=[0.0, 0.0, chosen], equivalent_yaw_deg=other, closure_yaw_candidates_deg=candidates,
                closing_to_long_axis_deg=round(axis_difference(chosen, yaw), 1))
    point = None
    if grasp_point is not None:
        gx, gy = float(grasp_point[0]), float(grasp_point[1])
        ux, uy = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
        dx, dy = gx - centre[0], gy - centre[1]
        along, across = dx * ux + dy * uy, -dx * uy + dy * ux
        point = dict(grasp_point_m=[round(float(v), 4) for v in grasp_point], reason=reason,
                     along_long_axis_m=round(along, 4), across_long_axis_m=round(across, 4),
                     within_footprint=abs(along) <= length / 2 + 0.005 and abs(across) <= width / 2 + 0.005)
    label = object_id or "object"
    tilt_text = (", tilt %.0f deg" % tilt) if _num(tilt) else ""
    prompt = ("Grasp %s facts: centre %s, top %s, bottom %s, length %.3f, width %.3f m, yaw %.0f deg%s. Jaws: angles_deg "
              "%s closes across the long axis (equivalent yaw %s); %s. Pad height reference: centre z %.3f. The grasp "
              "point is yours to choose from the image and these numbers (for example a tool's handle rather than its "
              "geometric centre); state it and the reason."
              % (label, json.dumps(facts["centre_m"]), facts["top_z"], facts["bottom_z"], length, width, yaw, tilt_text,
                 json.dumps(jaws["angles_deg"]), other,
                 "width fits the 88.8 mm opening" if fit == "ok" else notes[0], pad_z))
    return dict(object=object_id, facts=facts, jaws=jaws, max_opening_m=MAX_OPENING_M, opening_check=fit,
                pad_z_reference_m=round(pad_z, 4), length_width_ratio=round(ratio, 2) if ratio else None,
                elongated=elongated, chosen_point=point, notes=notes, self_prompt=prompt,
                basis="object table (VGGT estimate), not a measurement; grasp point chosen by the actor")


def _posture_transition_angles(release_yaw_deg, source_posture, target_posture):
    """计算 lying <-> upright 3D 姿态转换的完整 angles_deg=[pitch, roll, yaw]（绕夹爪闭合轴 ±Y 旋转 90°）。"""
    from ...robot.model import roboprobe_pose
    src = source_posture or ("lying" if target_posture == "upright" else "upright")
    dst = target_posture or src
    if src == dst:
        alt_yaw = round(wrap180(release_yaw_deg + 180.0), 1)
        return dict(
            posture_transition="%s->%s" % (src, dst),
            release_angles_deg=[0.0, 0.0, round(float(release_yaw_deg), 1)],
            alternative_angles_deg=[0.0, 0.0, alt_yaw],
        )
    q_base = roboprobe_pose.angles_to_quat(0.0, 0.0, float(release_yaw_deg))
    candidates = []
    for sign in (1.0, -1.0):
        q_rot = roboprobe_pose.quat_multiply(q_base, roboprobe_pose._axis_quat(1, sign * (math.pi / 2.0)))
        pitch, roll, yaw = roboprobe_pose.quat_to_angles(q_rot)
        t_axis = roboprobe_pose.tool_axis(q_rot)
        candidates.append((
            float(t_axis[1]),
            [round(wrap180(pitch), 1), round(float(roll), 1), round(wrap180(yaw), 1)],
            [round(float(v), 3) for v in t_axis],
        ))
    # 优先选择夹爪前向轴 (+X) 朝向工作台前方 (+Y) 而非朝向机械臂底座 (-Y) 的无缠绕主分支解
    candidates.sort(key=lambda item: item[0], reverse=True)
    best_angles, best_axis = candidates[0][1], candidates[0][2]
    alt_angles = candidates[1][1]
    return dict(
        posture_transition="%s->%s" % (src, dst),
        release_angles_deg=best_angles,
        alternative_angles_deg=alt_angles,
        tool_approach_axis_world=best_axis,
    )


def place_conversion(target_yaw, *, gripper_yaw_now=None, object_yaw_now=None, object_yaw_at_grasp=None,
                     gripper_yaw_at_grasp=None, source_posture=None, target_posture=None):
    """Gripper yaw (and optional 3D angles_deg for lying<->upright) at release. Pure function."""
    if object_yaw_now is not None and gripper_yaw_now is not None:
        delta = wrap90(float(target_yaw) - float(object_yaw_now))
        base, source = float(gripper_yaw_now), "current gripper yaw + (target - current object yaw)"
    elif object_yaw_at_grasp is not None and gripper_yaw_at_grasp is not None:
        delta = wrap90(float(target_yaw) - float(object_yaw_at_grasp))
        base, source = float(gripper_yaw_at_grasp), "gripper yaw at grasp + (target - object yaw at grasp)"
    else:
        raise ValueError("pose_plan_place_needs_object_yaw_now_with_arm_or_yaws_at_grasp")
    release = wrap180(base + delta)
    alternative = wrap180(release + 180.0)
    reference = gripper_yaw_now if gripper_yaw_now is not None else base
    chosen = nearest([round(release, 1), round(alternative, 1)], reference)
    out = dict(release_yaw_deg=chosen, alternative_yaw_deg=round(alternative if chosen == round(release, 1)
               else release, 1), rotation_from_current_deg=(round(wrap180(chosen - gripper_yaw_now), 1)
               if gripper_yaw_now is not None else None), method=source)
    if source_posture is not None or target_posture is not None:
        out.update(_posture_transition_angles(chosen, source_posture, target_posture))
    return out


# ------------------------------------------------------------------ runtime access (never raises)
def _latest_observation_id(runtime):
    latest = getattr(runtime, "latest", None)
    return latest.get("observation_id") if isinstance(latest, dict) else None


def attached_table(runtime, observation_id):
    """Object table attached to observation_id by obs3d (features.object_table_observe); None when absent."""
    from .obs3d import _memo
    entry = _memo(runtime, observation_id).get("object_table")
    return entry[0] if entry else None


def measured_yaw(runtime, arm):
    """angles_deg yaw of the arm's measured link6 pose in the latest observation; None when unavailable."""
    if arm not in ("left", "right"):
        return None
    try:
        from ...robot.model import roboprobe_pose
        latest = runtime.latest
        _, data = runtime.store.resolve_artifact(latest["robot_state_ref"], allowed_scopes=("actor",))
        pose = json.loads(data)["arms"][arm]["ee_pose"]
        x, y, z, w = pose["quaternion_xyzw"]
        return round(roboprobe_pose.pose_to_values([*pose["position_m"], w, x, y, z])["yaw_deg"], 1)
    except Exception:
        return None


def _event(runtime, name, payload, correlation):
    try:
        runtime.store.event(name, payload, correlation, world_kind="internal")
    except Exception:
        pass


def _grasp_memory(runtime):
    memory = getattr(runtime, "_pose_skill_grasps", None)
    if not isinstance(memory, dict):
        memory = {}
        try:
            runtime._pose_skill_grasps = memory
        except Exception:
            pass
    return memory


# ------------------------------------------------------------------ handler
def _targets(runtime, request, correlation):
    needed = request.get("needed")
    if not isinstance(needed, bool):
        raise ValueError("pose_plan_targets_requires_needed_boolean")
    items = request.get("targets") or []
    if needed and not items:
        raise ValueError("pose_plan_targets_needed_without_targets")
    normalised, lines = [], []
    for item in items:
        yaw = item.get("yaw_deg")
        entry = dict(object=item["object"], posture=item["posture"],
                     yaw_deg=round(wrap90(yaw), 1) if _num(yaw) else None,
                     tolerance_deg=float(item.get("tolerance_deg", 10.0)))
        for key in ("destination", "pitch_deg", "roll_deg", "basis"):
            if key in item:
                entry[key] = item[key]
        normalised.append(entry)
        parts = []
        if entry["yaw_deg"] is not None:
            parts.append("long axis yaw %.0f+/-%.0f deg" % (entry["yaw_deg"], entry["tolerance_deg"]))
        if entry["posture"] != "any":
            parts.append(entry["posture"])
        for key in ("pitch_deg", "roll_deg"):
            if _num(entry.get(key)):
                parts.append("%s %.0f" % (key[:-4], entry[key]))
        lines.append("%s%s: %s" % (entry["object"], (" -> " + entry["destination"]) if entry.get("destination") else "",
                                   ", ".join(parts) or "orientation free"))
    attention = ("End rotations (absolute, world): " + "; ".join(lines) +
                 ". The final waypoint of each transport must reach its rotation; before release use pose_plan "
                 "kind='place' for the gripper yaw.") if needed else None
    if attention and len(attention) > 1600:
        attention = attention[:1597] + "..."
    result = dict(kind="targets", needed=needed, reason=request.get("reason"), targets=normalised,
                  attention_text=attention,
                  note=("Keep attention_text in task_plan attention." if needed else
                        "Orientation not constrained; grasp suggestions remain available."))
    _event(runtime, EVENT_TARGETS, dict(needed=needed, reason=request.get("reason"), targets=normalised,
                                        observation_id=_latest_observation_id(runtime)), correlation)
    return result


def _grasp(runtime, request, correlation):
    latest = _latest_observation_id(runtime)
    observation_id = request.get("observation_id") or latest
    object_id = request.get("object")
    row, source = request.get("row"), "copied_row"
    if row is None:
        if not object_id:
            raise ValueError("pose_plan_grasp_requires_object_or_row")
        table = attached_table(runtime, observation_id)
        if table is None:
            raise ValueError("pose_plan_no_attached_table_for_observation: use the latest observation's id or copy "
                             "the row values into request.row")
        row = row_from_table(table, object_id)
        if row is None:
            raise ValueError("pose_plan_object_not_in_attached_table:" + str(object_id))
        source, support_z = "attached_table", table.get("table_z")
    else:
        support_z = None
    if not _num(row.get("yaw_deg")) or not _num(row.get("length")) or not _num(row.get("width")):
        raise ValueError("pose_plan_row_needs_numeric_length_width_yaw")
    arm = request.get("arm")
    yaw_now = measured_yaw(runtime, arm)
    point = request.get("grasp_point_m")
    if point is not None and (not isinstance(point, list) or len(point) not in (2, 3) or not all(map(_num, point))):
        raise ValueError("pose_plan_grasp_point_m_needs_2_or_3_numbers")
    result = grasp_facts(row, measured_yaw=yaw_now, support_z=support_z, object_id=object_id, grasp_point=point,
                         reason=request.get("reason"))
    result.update(kind="grasp", observation_id=observation_id if source == "attached_table" else None,
                  row_source=source, arm=arm, measured_arm_yaw_deg=yaw_now)
    _grasp_memory(runtime)[str(object_id or "")] = dict(object_yaw_deg=result["facts"]["yaw_deg"],
                                                        gripper_yaw_deg=result["jaws"]["angles_deg"][2], arm=arm)
    _event(runtime, EVENT_GRASP, dict(object=object_id, observation_id=result["observation_id"], row_source=source,
                                      row=row, arm=arm, measured_arm_yaw_deg=yaw_now, jaws=result["jaws"],
                                      pad_z_reference_m=result["pad_z_reference_m"],
                                      opening_check=result["opening_check"], chosen_point=result["chosen_point"],
                                      latest_observation_id=latest), correlation)
    return result


def _place(runtime, request, correlation):
    arm = request.get("arm")
    yaw_now = measured_yaw(runtime, arm)
    remembered = _grasp_memory(runtime).get(str(request.get("object") or ""), {})
    gripper_at_grasp = request.get("gripper_yaw_at_grasp_deg", remembered.get("gripper_yaw_deg"))
    object_at_grasp = request.get("object_yaw_at_grasp_deg", remembered.get("object_yaw_deg"))
    target_yaw = request.get("target_yaw_deg")
    if not _num(target_yaw):
        if request.get("target_posture") == "upright":
            target_yaw = object_at_grasp if _num(object_at_grasp) else (
                request.get("object_yaw_now_deg") if _num(request.get("object_yaw_now_deg")) else 0.0
            )
        else:
            raise ValueError("pose_plan_place_requires_target_yaw_deg")
    conversion = place_conversion(target_yaw, gripper_yaw_now=yaw_now,
                                  object_yaw_now=request.get("object_yaw_now_deg") if yaw_now is not None else None,
                                  object_yaw_at_grasp=object_at_grasp, gripper_yaw_at_grasp=gripper_at_grasp,
                                  source_posture=request.get("source_posture"),
                                  target_posture=request.get("target_posture"))
    result = dict(kind="place", object=request.get("object"), arm=arm, target_yaw_deg=round(wrap90(
        target_yaw), 1), measured_arm_yaw_deg=yaw_now, **conversion,
        note="Keep the grasp's pitch/roll unless the end posture changes; lying -> upright needs a 90 deg rotation "
             "about the jaw closing line. Lower the object close to the support before opening.")
    _event(runtime, EVENT_PLACE, dict(request=request, result=result, observation_id=_latest_observation_id(runtime)),
           correlation)
    return result


def handle(runtime, args, correlation):
    """Tool handler: dispatch request.kind; invalid requests raise ValueError like the other read tools."""
    from ...core.tool_output import ToolOutput
    request = args.get("request")
    if not isinstance(request, dict):
        raise ValueError("pose_plan_requires_request_object")
    kind = request.get("kind")
    if kind == "targets":
        result = _targets(runtime, request, correlation)
    elif kind == "grasp":
        result = _grasp(runtime, request, correlation)
    elif kind == "place":
        result = _place(runtime, request, correlation)
    else:
        raise ValueError("pose_plan_unknown_kind")
    return ToolOutput(result).response()
