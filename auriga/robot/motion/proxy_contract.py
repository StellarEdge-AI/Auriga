"""Validation of caller-supplied geometry estimates, without renderer or FK."""
from copy import deepcopy
import math
import numpy as np


def _vec(value):
    a = np.asarray(value, dtype=float)
    if a.shape != (3,) or not np.isfinite(a).all():
        raise ValueError("motion_proxy_requires_finite_xyz")
    return a


def validate_boxes(items):
    if not isinstance(items, list) or len(items) > 40:
        raise ValueError("motion_review_requires_at_most_40_boxes")
    result = []
    for item in items:
        if not isinstance(item, dict) or set(item) - {"id", "min_m", "max_m", "margin_m", "evidence"}:
            raise ValueError("invalid_motion_proxy_fields")
        low, high = _vec(item["min_m"]), _vec(item["max_m"])
        if np.any(high <= low) or np.any(np.abs([low, high]) > 3):
            raise ValueError("invalid_motion_proxy_bounds")
        margin = item.get("margin_m", .01)
        if type(margin) not in (int, float) or not math.isfinite(margin) or not 0 <= margin <= .2:
            raise ValueError("invalid_motion_proxy_margin")
        if not isinstance(item.get("evidence"), str) or not 1 <= len(item["evidence"]) <= 1000:
            raise ValueError("motion_proxy_requires_observation_evidence")
        identifier = item.get("id")
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 160:
            raise ValueError("motion_proxy_requires_id")
        if identifier.startswith("native_static/"):
            raise ValueError("motion_proxy_id_uses_reserved_native_namespace")
        if identifier in {x["id"] for x in result}:
            raise ValueError("duplicate_motion_proxy_id")
        result.append(dict(id=identifier, min_m=low.tolist(), max_m=high.tolist(),
                           margin_m=float(margin), evidence=item["evidence"], source_kind="actor_geometry_estimate"))
    return result


def validate_proxies(value):
    if not isinstance(value, dict) or set(value)-{'obstacles', 'held_objects', 'contact_entity_id', 'initial_contact', 'placement_contact'}:
        raise ValueError('invalid_batch_review_proxies')
    result = dict(obstacles=[{k:v for k,v in box.items() if k != 'source_kind'} for box in validate_boxes(value.get('obstacles', []))], held_objects={})
    held = value.get('held_objects', {})
    if not isinstance(held, dict) or set(held)-{'left', 'right'}:
        raise ValueError('held_objects_requires_left_or_right')
    for arm, box in held.items():
        if not isinstance(box, dict) or set(box)-{'min_m', 'max_m', 'margin_m', 'evidence'}:
            raise ValueError('invalid_held_object_proxy')
        result['held_objects'][arm] = {k:v for k,v in validate_boxes([dict(box, id='held_'+arm)])[0].items() if k not in ('id', 'source_kind')}
    known = {box['id'] for box in result['obstacles']}
    target = value.get('contact_entity_id')
    if target is not None and (not isinstance(target, str) or target not in known):
        raise ValueError('contact_entity_id_requires_supplied_obstacle')
    initial = value.get('initial_contact')
    if initial is not None:
        if (not isinstance(initial, dict) or set(initial)-{'entity_ids', 'evidence'}
                or not isinstance(initial.get('entity_ids'), list) or not initial['entity_ids']
                or any(not isinstance(identifier,str) or identifier not in known for identifier in initial['entity_ids'])
                or not isinstance(initial.get('evidence'), str) or not initial['evidence'].strip()):
            raise ValueError('invalid_initial_contact_declaration')
    placement = value.get('placement_contact')
    if placement is not None:
        if (not isinstance(placement, dict) or set(placement)-{'entity_id', 'evidence', 'max_penetration_m'}
                or not isinstance(placement.get('entity_id'),str) or placement['entity_id'] not in known or not isinstance(placement.get('evidence'), str)
                or not placement['evidence'].strip()):
            raise ValueError('invalid_placement_contact_declaration')
        tolerance = placement.get('max_penetration_m', .002)
        if type(tolerance) not in (int, float) or not math.isfinite(tolerance) or not 0 <= tolerance <= .003:
            raise ValueError('invalid_placement_contact_tolerance')
    for key in ('contact_entity_id', 'initial_contact', 'placement_contact'):
        if key in value:
            result[key] = deepcopy(value[key])
    return result
