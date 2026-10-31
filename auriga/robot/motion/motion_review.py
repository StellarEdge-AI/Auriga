"""Full-link motion review of a cached plan against explicitly supplied proxies.

Robot spheres are the native planner model; finger boxes conservatively cover
measured-to-commanded jaw openings. Scene boxes are observed/estimated proxies,
not simulator truth.
"""
import itertools

import numpy as np


def _cover_box(low, high, spacing=.025):
    """Sphere cells whose union covers every point of a local AABB."""
    low, high = np.array(low), np.array(high)
    counts = np.maximum(1, np.ceil((high-low)/spacing).astype(int))
    if np.prod(counts) > 2000:
        raise ValueError("motion_proxy_cover_too_large")
    size = (high-low)/counts
    centers = np.array(list(itertools.product(*[low[i]+(np.arange(counts[i])+.5)*size[i] for i in range(3)])))
    return centers, np.full(len(centers), np.linalg.norm(size)/2)


def _segment_box_clearance(a, b, low, high, radii):
    """Exact chord-to-AABB distance minus spherical/curvature padding.

    Squared point-to-box distance is quadratic between the six face crossing
    times. Minimize each piece instead of treating rounded corners as a cube.
    """
    direction = b-a
    parallel = np.abs(direction) < 1e-14
    safe = np.where(parallel, 1., direction)
    crossings = np.concatenate(((low-a)/safe, (high-a)/safe), axis=1)
    crossings = np.where(np.concatenate((parallel, parallel), axis=1), 0., crossings)
    knots = np.sort(np.clip(np.concatenate((np.zeros((len(a), 1)), crossings,
                                            np.ones((len(a), 1))), axis=1), 0., 1.), axis=1)
    left, right = knots[:, :-1], knots[:, 1:]
    middle = a[:, None, :] + ((left+right)/2)[:, :, None]*direction[:, None, :]
    below, above = middle < low, middle > high
    offsets = np.where(below, a[:, None, :]-low, np.where(above, a[:, None, :]-high, 0.))
    slopes = np.where(below | above, direction[:, None, :], 0.)
    optimum = -np.sum(offsets*slopes, axis=2)/np.maximum(np.sum(slopes*slopes, axis=2), 1e-30)
    points = a[:, None, :] + np.clip(optimum, left, right)[:, :, None]*direction[:, None, :]
    distance = np.linalg.norm(points-np.clip(points, low, high), axis=2).min(axis=1)
    return distance-radii
