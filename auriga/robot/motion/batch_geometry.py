"""Advisory proxy review of both moving arms on the cached shared timeline.

All geometry comes from declared world boxes, public robot meshes/configuration
and FK from public measurements. No simulator task geometry is queried.
"""
from copy import deepcopy
import itertools
import math

import numpy as np

from .motion_review import _cover_box, _segment_box_clearance
from .proxy_contract import validate_boxes, validate_proxies


def annotate_overlay(camera, rendered, metadata, trajectory):
    """Show declared obstacle boxes and each panel's hypothetical held volume."""
    import cv2
    from ..model.geometry import project
    review = trajectory.get('batch_geometry_review', {})
    boxes, held = review.get('obstacles', []), review.get('held_objects', {})
    width, height = metadata['panel_width'], metadata['panel_height']
    by_step = {s['native_step']: s for s in trajectory['samples']}
    initial = trajectory['samples'][0]
    edges = [(i,j) for i in range(8) for j in range(i+1,8) if (i^j) in (1,2,4)]
    for panel_index, panel in enumerate(metadata['panels']):
        current = by_step[panel['native_step']]
        shapes = [(box['id'], np.array(list(itertools.product(*zip(box['min_m'],box['max_m'])))), (210,60,220)) for box in boxes]
        for arm, box in held.items():
            first = initial['arms'][arm].get('link6_pose'); pose = current['arms'][arm].get('link6_pose')
            if first is None or pose is None:
                continue
            corners = np.array(list(itertools.product(*zip(box['min_m'],box['max_m']))))
            local = (corners-np.asarray(first['position_m'])) @ np.asarray(first['rotation_matrix'])
            world = local @ np.asarray(pose['rotation_matrix']).T+np.asarray(pose['position_m'])
            shapes.append((arm+'/held?',world,(60,220,120)))
        image = rendered[:,panel_index*width:(panel_index+1)*width]
        for label, corners, color in shapes:
            pixels, depth, _ = project(camera, corners)
            pixels = np.clip(np.rint(pixels),-100000,100000).astype(int)
            for i,j in edges:
                if depth[i] <= 0 or depth[j] <= 0:
                    continue
                visible,a,b = cv2.clipLine((0,0,width,height),tuple(pixels[i]),tuple(pixels[j]))
                if visible:
                    cv2.line(image,a,b,color,1,cv2.LINE_AA)
            visible = np.flatnonzero((depth>0)&(pixels[:,0]>=0)&(pixels[:,0]<width)&(pixels[:,1]>=56)&(pixels[:,1]<height-38))
            if len(visible):
                cv2.putText(image,label[:32],tuple(pixels[visible[0]]),cv2.FONT_HERSHEY_SIMPLEX,.35,color,1,cv2.LINE_AA)
        cv2.rectangle(image,(0,height-18),(width-1,height-1),(18,18,18),-1)
        cv2.putText(image,'Declared proxies; held attachment and physical clearance unknown',(6,height-7),
            cv2.FONT_HERSHEY_SIMPLEX,.32,(255,255,255),1,cv2.LINE_AA)
    metadata.update(proxy_overlay=dict(obstacle_boxes=len(boxes),held_object_arms=sorted(held)),
        limitations='Exact cached command FK on original observed RGB, plus declared world boxes and hypothetical rigid held volumes. '
            'No future measurement, visibility, physical collision/contact or attachment proof.')
    return rendered, metadata


def review_batch(adapter, before, trajectory, proxies):
    from .trajectory_evidence import _hardware, _bases, _joint_state, _sphere_set
    from ..model.geometry import fk
    from ..model.robot_model import collision_mesh_bounds
    proxies = validate_proxies(proxies)
    result = dict(status='unavailable', assessment_role='advisory', collision_free_proven=False,
        obstacles=proxies['obstacles'], held_objects=proxies['held_objects'], risks=[],
        minimum_estimated_clearance_m=None, contact_declarations={key: proxies[key] for key in
            ('contact_entity_id', 'initial_contact', 'placement_contact') if key in proxies},
        contact_semantics='Declarations annotate estimated overlaps; they never suppress a risk or prove physical contact.',
        unknown=['Unseen/omitted scene geometry and task contact/retention are unknown.',
            'Held proxies assume rigid attachment; slip, release, shared holding and regrasp invalidate that physical assumption.',
            'Chord/curvature estimates from commanded joint rows do not prove actual continuous-time clearance.'])
    if not proxies['obstacles']:
        result['unknown'].append('No task-object obstacle boxes were supplied.')
    if not proxies['held_objects']:
        result['unknown'].append('No held-object extents were supplied.')
    try:
        hardware = _hardware(adapter)
        model, spheres = hardware['model'], deepcopy(hardware['spheres'])
        bases = _bases(model, before, trajectory['frame_id'], trajectory['tcp_config_id'])
        try:
            fingers = collision_mesh_bounds(adapter.root/'Assets/Robots/x5/X5A.urdf')
            for link, box in fingers.items():
                centers, radii = _cover_box(box['min_m'], box['max_m'], spacing=.04)
                spheres.setdefault(link, []).extend(dict(center=c.tolist(), radius=float(r)) for c,r in zip(centers,radii))
            result['finger_geometry'] = 'public STL local AABB sphere cover at each commanded opening'
        except (OSError, ValueError, KeyError) as error:
            result['finger_geometry'] = 'unavailable'
            result['unknown'].append('Finger mesh cover unavailable: '+type(error).__name__)
        held_local, previous, previous_sample = {}, None, None
        minimum, risks, checkpoints = math.inf, {}, []
        reach = sum(float(np.linalg.norm(np.asarray(j['T_parent_joint'])[:3,3])) for j in model['joints'])
        reach += max(float(np.linalg.norm(s['center'])+s['radius']) for v in spheres.values() for s in v)
        for sample in trajectory['samples']:
            sets, inflation = {}, {}
            for arm in ('left', 'right'):
                command = sample['arms'][arm]
                poses = fk({**model, 'T_world_base': bases[arm]},
                    _joint_state(before['arms'][arm], command, hardware['jaws'], initial=sample['native_step']==0))
                centers, radii, labels = _sphere_set(spheres, poses, hardware['sphere_buffer_m'])
                labels = [arm+'/'+label['link_name'] for label in labels]
                extent = 0.
                if arm in proxies['held_objects']:
                    box = proxies['held_objects'][arm]
                    if arm not in held_local:
                        corners = np.array(list(itertools.product(*zip(box['min_m'], box['max_m']))))
                        local = (corners-poses['link6'][:3,3]) @ poses['link6'][:3,:3]
                        held_local[arm] = _cover_box(local.min(axis=0)-box['margin_m'], local.max(axis=0)+box['margin_m'], spacing=.05)
                        if len(held_local[arm][1]) > 500:
                            raise ValueError('held_object_proxy_review_budget_exceeded')
                    local, sizes = held_local[arm]
                    world = local @ poses['link6'][:3,:3].T + poses['link6'][:3,3]
                    centers = np.vstack((centers, world)); radii = np.r_[radii, sizes]
                    labels += [arm+'/held_object']*len(sizes)
                    extent = float(np.max(np.linalg.norm(local,axis=1)+sizes))
                delta = (0. if previous_sample is None else float(np.abs(np.asarray(command['q_rad'])-
                    previous_sample['arms'][arm]['q_rad']).sum()))
                inflation[arm] = (reach+extent)*delta*delta/8+.002
                sets[arm] = centers,radii,labels
                old = centers if previous is None else previous[arm][0]
                for box in proxies['obstacles']:
                    gaps = _segment_box_clearance(old, centers, np.array(box['min_m']), np.array(box['max_m']),
                        radii+box['margin_m']+inflation[arm])
                    index = int(np.argmin(gaps)); gap = float(gaps[index]); minimum = min(minimum,gap)
                    if gap <= 0:
                        key = labels[index], box['id']
                        item = dict(kind='scene_proxy_intersection', moving_part=key[0], obstacle=key[1],
                            native_step=sample['native_step'], estimated_clearance_m=gap)
                        if key[1] == proxies.get('contact_entity_id'):
                            item['contact_declaration'] = 'caller_selected_contact_entity; overlap retained'
                        if key not in risks or gap < risks[key]['estimated_clearance_m']:
                            risks[key] = item
            left, lr, ll = sets['left']; right, rr, rl = sets['right']
            current_relative = left[:,None,:]-right[None,:,:]
            old_relative = current_relative if previous is None else previous['left'][0][:,None,:]-previous['right'][0][None,:,:]
            direction = current_relative-old_relative
            fraction = np.clip(-np.sum(old_relative*direction,axis=2)/np.maximum(np.sum(direction*direction,axis=2),1e-30),0,1)
            gaps = np.linalg.norm(old_relative+fraction[:,:,None]*direction,axis=2)-lr[:,None]-rr[None,:]-sum(inflation.values())
            i,j = np.unravel_index(np.argmin(gaps),gaps.shape)
            gap = float(gaps[i,j]); minimum=min(minimum,gap)
            if gap <= 0:
                key=ll[i],rl[j]
                item=dict(kind='inter_arm_proxy_intersection',moving_part=key[0],obstacle=key[1],
                    native_step=sample['native_step'],estimated_clearance_m=gap)
                if key not in risks or gap < risks[key]['estimated_clearance_m']:
                    risks[key]=item
            previous,previous_sample=sets,sample
        result.update(status='risks_detected' if risks else 'conditional', risks=list(risks.values()),
            minimum_estimated_clearance_m=None if not math.isfinite(minimum) else minimum,
            sampled_boundaries=len(trajectory['samples']),
            semantics='Both arms move on the same cached timeline; relative sphere chords include both arm motions and hypothetical rigid held proxies.',
            geometry_source='public robot configuration/URDF/STL + measured inferred bases + caller world AABBs')
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ArithmeticError, ImportError) as error:
        result['unknown'].append('Geometry review unavailable: '+str(error))
        result['reason']=type(error).__name__
    return result
