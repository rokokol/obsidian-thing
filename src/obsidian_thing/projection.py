from collections.abc import Mapping

import numpy as np

AXES = 2


# Paths sort the rows and both axes share one scale, so the same vectors always draw the same map
# and a distance on the screen stays comparable to the distance it stands for
def project(vectors: Mapping[str, list[float]]) -> tuple[dict[str, tuple[float, float]], list[float]]:
    paths = sorted(vectors)
    if not paths:
        return {}, [0.0] * AXES
    matrix = np.asarray([vectors[path] for path in paths], dtype=np.float64)
    if not np.isfinite(matrix).all():
        raise ValueError('The vectors hold an invalid number')
    centred = matrix - matrix.mean(axis=0, keepdims=True)
    axes = np.zeros((len(paths), AXES))
    explained = [0.0] * AXES
    if len(paths) > 1:
        try:
            _, singular, components = np.linalg.svd(centred, full_matrices=False)
        except np.linalg.LinAlgError:
            raise ValueError('The vectors cannot be reduced') from None
        # A singular vector keeps an arbitrary sign; the largest loading fixes it so that
        # a library update cannot mirror a finished map
        for index, row in enumerate(components):
            largest = int(np.argmax(np.abs(row)))
            if row[largest] < 0:
                components[index] = -row
        used = components[:AXES]
        axes[:, :len(used)] = centred @ used.T
        total = float((singular ** 2).sum())
        if total:
            share = (singular ** 2)[:AXES] / total
            explained = [round(float(value), 6) for value in share] + [0.0] * (AXES - len(share))
    scale = float(np.abs(axes).max())
    if scale:
        axes /= scale
    return {path: (_round(float(x)), _round(float(y)))
            for path, (x, y) in zip(paths, axes, strict=True)}, explained


def _round(value: float) -> float:
    # A negative zero travels into JSON as -0.0 and reads as a signed number to a client
    return round(value, 5) + 0.0
