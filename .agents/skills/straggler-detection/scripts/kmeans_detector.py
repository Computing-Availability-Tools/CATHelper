from typing import List, Tuple, Optional
import math
import random
INVALID_MARKER = -99999

def general_anomaly_detection(
    ranks: List[int],
    values: List[float],
    anomaly_multiplier: float = 2.0,
    high_is_anomaly: bool = True,
    max_k: int = 10,
    max_iter: int = 300,
    max_depth: int = 10,
    convergence_eps: float = 1e-9,
    seed: int = 42,
) -> Tuple[List[int], List[float]]:
    if not values or len(values) < 2:
        return [], []
    if len(ranks) != len(values):
        n = min(len(ranks), len(values))
        ranks = list(ranks[:n])
        values = list(values[:n])
    rng = random.Random(seed)
    first_baseline, anomaly_ids = _recurse_anomaly(
        list(values), list(range(len(values))), 0,
        anomaly_multiplier, high_is_anomaly,
        max_k, max_iter, max_depth, convergence_eps, rng,
    )
    if not anomaly_ids:
        return [], []
    denom = first_baseline if first_baseline and first_baseline > 0 else 1.0
    anomaly_ranks = []
    degradations = []
    for idx in anomaly_ids:
        if 0 <= idx < len(ranks):
            anomaly_ranks.append(ranks[idx])
            if high_is_anomaly:
                degradations.append(values[idx] / denom)
            else:
                degradations.append(denom / values[idx] if values[idx] > 0 else 1.0)
    return anomaly_ranks, degradations

def _recurse_anomaly(
    data: List[float],
    indices: List[int],
    depth: int,
    anomaly_multiplier: float,
    high_is_anomaly: bool,
    max_k: int,
    max_iter: int,
    max_depth: int,
    convergence_eps: float,
    rng: random.Random,
) -> Tuple[Optional[float], List[int]]:
    valid_data = []
    valid_indices = []
    for v, i in zip(data, indices):
        if v > 0 and v != INVALID_MARKER:
            valid_data.append(v)
            valid_indices.append(i)
    if len(valid_data) < 2:
        return None, []
    anomaly_vals, anomaly_ids, baseline_mean = _kmeans_anomaly_detect(
        valid_data, valid_indices, anomaly_multiplier, high_is_anomaly,
        max_k, max_iter, convergence_eps, rng,
    )
    if not anomaly_vals:
        return baseline_mean, []
    if depth >= max_depth:
        return baseline_mean, list(anomaly_ids)
    _, sub_ids = _recurse_anomaly(
        anomaly_vals, list(anomaly_ids), depth + 1,
        anomaly_multiplier, high_is_anomaly,
        max_k, max_iter, max_depth, convergence_eps, rng,
    )
    if sub_ids:
        return baseline_mean, sub_ids
    return baseline_mean, list(anomaly_ids)

def _kmeans_anomaly_detect(
    data: List[float],
    indices: List[int],
    anomaly_multiplier: float,
    high_is_anomaly: bool,
    max_k: int,
    max_iter: int,
    convergence_eps: float,
    rng: random.Random,
) -> Tuple[List[float], List[int], float]:
    n = len(data)
    if n < 2:
        return [], [], 0.0
    zdata = _zscore(data, convergence_eps)
    k = _elbow_optimal_k(zdata, max_k, max_iter, convergence_eps, rng)
    labels, centers = _kmeans(zdata, k, max_iter, convergence_eps, rng)
    cluster_means = {}
    for c in range(k):
        members = [data[j] for j in range(n) if labels[j] == c]
        if members:
            cluster_means[c] = sum(members) / len(members)
    if not cluster_means:
        return [], [], 0.0
    if high_is_anomaly:
        baseline_mean = min(cluster_means.values())
    else:
        baseline_mean = max(cluster_means.values())
    anomaly_clusters = []
    if high_is_anomaly:
        for c in sorted(cluster_means, key=lambda x: cluster_means[x], reverse=True):
            if cluster_means[c] > baseline_mean * anomaly_multiplier:
                anomaly_clusters.append(c)
            else:
                break
    else:
        for c in sorted(cluster_means, key=lambda x: cluster_means[x]):
            if cluster_means[c] < baseline_mean and \
                    cluster_means[c] / baseline_mean < 1.0 / anomaly_multiplier:
                anomaly_clusters.append(c)
            else:
                break
    if not anomaly_clusters:
        return [], [], baseline_mean
    anomaly_vals = []
    anomaly_ids = []
    for j in range(n):
        if labels[j] in anomaly_clusters:
            anomaly_vals.append(data[j])
            anomaly_ids.append(indices[j])
    return anomaly_vals, anomaly_ids, baseline_mean

def _zscore(data: List[float], eps: float) -> List[float]:
    if not data:
        return []
    mean = sum(data) / len(data)
    var = sum((x - mean) ** 2 for x in data) / len(data)
    std = math.sqrt(var)
    if std < eps:
        std = 1.0
    return [(x - mean) / std for x in data]

def _elbow_optimal_k(
    data: List[float],
    max_k: int,
    max_iter: int,
    convergence_eps: float,
    rng: random.Random,
) -> int:
    n = len(data)
    upper = min(n, max_k)
    if upper < 2:
        return 2
    inertias = {}
    for k in range(2, upper + 1):
        _, _, inertia = _kmeans(data, k, max_iter, convergence_eps, rng, want_inertia=True)
        inertias[k] = inertia
    if len(inertias) < 2:
        return 2
    ks = sorted(inertias.keys())
    scores = {}
    for i in range(1, len(ks) - 1):
        prev, cur, nxt = ks[i - 1], ks[i], ks[i + 1]
        d1 = inertias[cur] - inertias[prev]
        d2 = inertias[nxt] - inertias[cur]
        scores[cur] = d1 - d2
    if not scores:
        return 2
    return max(scores, key=scores.get)

def _kmeans(
    data: List[float],
    k: int,
    max_iter: int,
    convergence_eps: float,
    rng: random.Random,
    want_inertia: bool = False,
):
    n = len(data)
    if k <= 1 or n < 2:
        labels = [0] * n
        centers = [sum(data) / n]
        if want_inertia:
            return labels, centers, 0.0
        return labels, centers
    if k > n:
        k = n
    init_idx = _kmeans_pp_seed(data, k, rng)
    centers = [data[i] for i in init_idx]
    labels = [0] * n
    for _ in range(max_iter):
        new_labels = []
        assign_changed = False
        for x in data:
            best = 0
            best_d = float('inf')
            for c_idx, c in enumerate(centers):
                d = (x - c) ** 2
                if d < best_d:
                    best_d = d
                    best = c_idx
            new_labels.append(best)
            if best != labels[len(new_labels) - 1]:
                assign_changed = True
        sums = [0.0] * k
        cnts = [0] * k
        for j, x in enumerate(data):
            c = new_labels[j]
            sums[c] += x
            cnts[c] += 1
        new_centers = []
        for c in range(k):
            if cnts[c] > 0:
                new_centers.append(sums[c] / cnts[c])
            else:
                farthest_j = -1
                farthest_d = -1.0
                for j, x in enumerate(data):
                    d = (x - centers[c]) ** 2
                    if d > farthest_d:
                        farthest_d = d
                        farthest_j = j
                if farthest_j >= 0:
                    new_centers.append(data[farthest_j])
                else:
                    new_centers.append(centers[c])
        shift = max(abs(new_centers[c] - centers[c]) for c in range(k))
        centers = new_centers
        labels = new_labels
        if shift < convergence_eps and not assign_changed:
            break
    if want_inertia:
        inertia = 0.0
        for j, x in enumerate(data):
            inertia += (x - centers[labels[j]]) ** 2
        return labels, centers, inertia
    return labels, centers

def _kmeans_pp_seed(data: List[float], k: int, rng: random.Random) -> List[int]:
    n = len(data)
    indices = [0]
    centers = [data[0]]
    while len(indices) < k:
        dist_sq = []
        total = 0.0
        for x in data:
            min_d = min((x - c) ** 2 for c in centers)
            dist_sq.append(min_d)
            total += min_d
        if total <= 0:
            for i in range(n):
                if i not in indices:
                    indices.append(i)
                    centers.append(data[i])
                    break
            else:
                break
            continue
        r = rng.random() * total
        acc = 0.0
        chosen = -1
        for i, d in enumerate(dist_sq):
            acc += d
            if acc >= r:
                chosen = i
                break
        if chosen < 0:
            chosen = n - 1
        if chosen not in indices:
            indices.append(chosen)
            centers.append(data[chosen])
    return indices[:k]
