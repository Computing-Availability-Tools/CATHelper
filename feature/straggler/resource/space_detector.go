package resource

import (
	"math"
	"sort"
)

// =============================================================================
// Space (Peer-Comparison) Dimension Detection
// =============================================================================

// detectSpaceAnomalies computes per-time-point space scores for all cards
// across all metrics over the detection window.
//
// Returns: cardID → metric → []score (one per detection-window time point).
func detectSpaceAnomalies(
	detectionRows []CSVRow,
	baselines map[int]map[MetricName]*CardBaseline,
	cardIDs []int,
	cfg DetectionConfig,
) *SpaceDetectionResult {
	result := &SpaceDetectionResult{
		Scores: make(map[int]map[MetricName][]float64),
	}

	// Per-metric robust noise scale, self-calibrated from the historical
	// baselines (median across cards of 1.4826 × MAD). Used by the cluster
	// method to judge cluster significance in each metric's own noise units.
	scale := make(map[MetricName]float64, len(AllMetrics))
	for _, metric := range AllMetrics {
		scale[metric] = spaceMetricScale(metric, baselines, cardIDs)
	}

	// Init.
	for _, cid := range cardIDs {
		result.Scores[cid] = make(map[MetricName][]float64)
		for _, metric := range AllMetrics {
			result.Scores[cid][metric] = make([]float64, 0, len(detectionRows))
		}
	}

	// For each time point, for each metric, compute Z-Scores.
	for _, row := range detectionRows {
		for _, metric := range AllMetrics {
			meta := MetricMetaRegistry[metric]
			vals := getMetricValues(row, metric, cardIDs)

			// Filter to valid cards present at this time point.
			present := make([]int, 0, len(cardIDs))
			presentVals := make([]float64, 0, len(cardIDs))
			for i, cid := range cardIDs {
				dict := getMetricDict(row, metric)
				if dict == nil {
					continue
				}
				if _, ok := dict[cid]; ok {
					present = append(present, i)
					presentVals = append(presentVals, vals[i])
				}
			}

			if len(presentVals) < 2 {
				// Need at least 2 cards for peer comparison.
				for _, cid := range cardIDs {
					result.Scores[cid][metric] = append(result.Scores[cid][metric], 0)
				}
				continue
			}

			// Compute Z-Scores based on method.
			switch meta.SpaceMethod {
			case MethodAbsolute:
				// Absolute threshold: > threshold → anomaly.
				for i, cid := range cardIDs {
					z := 0.0
					if vals[i] > meta.AbsThreshold {
						z = 999 // sentinel for "absolute anomaly"
					}
					result.Scores[cid][metric] = append(result.Scores[cid][metric], z)
				}

			case MethodDirect:
				// Direct comparison (for freq): below min of others → anomaly.
				allVals := presentVals
				sort.Float64s(allVals)
				minVal := allVals[0]
				for i, cid := range cardIDs {
					z := 0.0
					if vals[i] < minVal || (vals[i] < minVal+cfg.FreqDownclockGap) {
						// If significantly below the minimum peer value.
						if vals[i] < minVal {
							z = 999
						}
					}
					result.Scores[cid][metric] = append(result.Scores[cid][metric], z)
				}

			case MethodIQR:
				sorted := make([]float64, len(presentVals))
				copy(sorted, presentVals)
				sort.Float64s(sorted)
				q1 := Percentile(sorted, 0.25)
				q3 := Percentile(sorted, 0.75)
				iqr := q3 - q1
				lower := q1 - cfg.SpaceIQRMult*iqr
				upper := q3 + cfg.SpaceIQRMult*iqr

				for i, cid := range cardIDs {
					z := 0.0
					if vals[i] < lower || vals[i] > upper {
						z = 999
					}
					result.Scores[cid][metric] = append(result.Scores[cid][metric], z)
				}

			case MethodCluster:
				// Majority-mode clustering (space): "whoever has the majority
				// is the peer norm". Recursively split at the largest gap into
				// a full partition to locate the majority (baseline) cluster,
				// then judge each NON-baseline card by its own deviation from
				// the baseline mean (per-point, one-sided by direction), in
				// units of the metric's historical noise.
				//
				// Baseline (majority) members are exempt — they ARE the normal
				// reference. This preserves the "spread fleet = normal" guard:
				// a fleet with no dominant gap is one cluster → everyone is in
				// the baseline → nobody is scored, so the edges of a normal
				// spread are never flagged even when each card is individually
				// stable (which would otherwise collapse the noise scale).
				zAtT := make(map[int]float64, len(cardIDs)) // cardID → z (0 if not flagged)
				if len(presentVals) >= 2 {
					sortedIdx := make([]int, len(present))
					for k := range present {
						sortedIdx[k] = k // index into present/presentVals
					}
					sort.Slice(sortedIdx, func(a, b int) bool {
						return presentVals[sortedIdx[a]] < presentVals[sortedIdx[b]]
					})
					clusters := gapSplitClusters(sortedIdx, presentVals)
					baseIdx := pickBaselineCluster(clusters, presentVals, meta.Direction)
					baseMean := clusterMean(clusters[baseIdx], presentVals)

					baseMembers := make(map[int]bool, len(clusters[baseIdx]))
					for _, k := range clusters[baseIdx] {
						baseMembers[k] = true
					}

					for pi, pv := range presentVals {
						if baseMembers[pi] {
							continue // majority = the reference, not judged
						}
						// One-sided: only the anomaly direction is checked.
						if (meta.Direction == DirHigh && pv <= baseMean) ||
							(meta.Direction == DirLow && pv >= baseMean) {
							continue
						}
						z := math.Abs(pv-baseMean) / scale[metric]
						if z > cfg.SpaceClusterK {
							zAtT[cardIDs[present[pi]]] = z
						}
					}
				}
				for _, cid := range cardIDs {
					result.Scores[cid][metric] = append(result.Scores[cid][metric], zAtT[cid])
				}

			default: // MethodZScore
				mean, std := MeanStd(presentVals)
				for i, cid := range cardIDs {
					z := 0.0
					if std > 0 {
						z = math.Abs(vals[i]-mean) / std
					}
					result.Scores[cid][metric] = append(result.Scores[cid][metric], z)
				}
			}
		}
	}

	return result
}

// =============================================================================
// MethodCluster helpers
// =============================================================================

// gapSplitClusters recursively splits the sorted present values at the largest
// adjacent gap, on BOTH sides, until no sub-group has a gap ≥ half its own
// span. It returns a full partition of index lists (indices into the values
// slice). This mirrors the Profiler homogenization clustering but decomposes
// both sides so all anomaly levels are separated from the normal core.
func gapSplitClusters(sortedIdx []int, vals []float64) [][]int {
	if len(sortedIdx) <= 1 {
		return [][]int{sortedIdx}
	}
	maxGap := -1.0
	splitPos := -1
	for i := 0; i < len(sortedIdx)-1; i++ {
		g := vals[sortedIdx[i+1]] - vals[sortedIdx[i]]
		if g > maxGap {
			maxGap = g
			splitPos = i
		}
	}
	span := vals[sortedIdx[len(sortedIdx)-1]] - vals[sortedIdx[0]]
	if span <= 0 || maxGap*2 < span {
		// All identical (span 0) or no dominant gap → no structure → one cluster.
		return [][]int{sortedIdx}
	}
	left := gapSplitClusters(sortedIdx[:splitPos+1], vals)
	right := gapSplitClusters(sortedIdx[splitPos+1:], vals)
	return append(left, right...)
}

// clusterMean returns the mean value of a cluster (list of indices into vals).
func clusterMean(idxList []int, vals []float64) float64 {
	if len(idxList) == 0 {
		return 0
	}
	var sum float64
	for _, k := range idxList {
		sum += vals[k]
	}
	return sum / float64(len(idxList))
}

// pickBaselineCluster selects the baseline: the largest cluster (the peer
// majority). On a member-count tie, the direction extreme is preferred —
// DirHigh → lowest mean, DirLow → highest mean.
func pickBaselineCluster(clusters [][]int, vals []float64, dir AnomalyDirection) int {
	maxCount := 0
	for _, cl := range clusters {
		if len(cl) > maxCount {
			maxCount = len(cl)
		}
	}
	best := -1
	for i, cl := range clusters {
		if len(cl) != maxCount {
			continue
		}
		if best == -1 {
			best = i
			continue
		}
		bMean := clusterMean(clusters[best], vals)
		cMean := clusterMean(cl, vals)
		if (dir == DirHigh && cMean < bMean) || (dir == DirLow && cMean > bMean) {
			best = i
		}
	}
	return best
}

// spaceMetricScale returns the robust noise scale for a metric, self-calibrated
// from the historical baselines: the median across cards of 1.4826 × MAD.
// Cards with zero historical MAD (constant values, e.g. idle util) are skipped;
// if every card is constant, a tiny floor avoids division by zero.
func spaceMetricScale(
	metric MetricName,
	baselines map[int]map[MetricName]*CardBaseline,
	cardIDs []int,
) float64 {
	var nonZero []float64
	for _, cid := range cardIDs {
		bl := baselines[cid][metric]
		if bl == nil || bl.N < 2 {
			continue
		}
		s := madToStdFactor * bl.Mad
		if s > 0 {
			nonZero = append(nonZero, s)
		}
	}
	if len(nonZero) == 0 {
		return 1e-3 // all historical values constant → tiny floor (hypersensitive)
	}
	return Median(nonZero)
}

// =============================================================================
// Space Score Aggregation
// =============================================================================

// aggregateSpaceScores reduces per-time-point space scores to per-card
// aggregate space scores.
//
// For each card+metric:
//   spaceScore = mean of Z-Scores across detection window
//   spaceAbnormal = mean Z-Score > threshold
func aggregateSpaceScores(space *SpaceDetectionResult, cardIDs []int, cfg DetectionConfig) map[int]map[MetricName]*MetricAnomalyDetail {
	result := make(map[int]map[MetricName]*MetricAnomalyDetail)

	for _, cid := range cardIDs {
		result[cid] = make(map[MetricName]*MetricAnomalyDetail)
		for _, metric := range AllMetrics {
			zscores := space.Scores[cid][metric]
			if len(zscores) == 0 {
				result[cid][metric] = &MetricAnomalyDetail{
					Metric:     metric,
					SpaceScore: 0,
				}
				continue
			}

			// For absolute/direct methods, consider "abnormal" if any point had sentinel value.
			meta := MetricMetaRegistry[metric]
			isSentinel := meta.SpaceMethod == MethodAbsolute || meta.SpaceMethod == MethodDirect
			isCluster := meta.SpaceMethod == MethodCluster

			var sum float64
			abnormalCount := 0
			for _, z := range zscores {
				if isSentinel {
					if z >= 999 {
						abnormalCount++
					}
				} else {
					sum += z
					if z > cfg.SpaceZThreshold {
						abnormalCount++
					}
				}
			}

			var spaceScore float64
			var spaceAbnormal bool
			switch {
			case isSentinel:
				// For absolute/direct: abnormal if >50% of points flagged.
				spaceScore = float64(abnormalCount) / float64(len(zscores))
				spaceAbnormal = spaceScore > 0.5
			case isCluster:
				// Cluster method: abnormal when the card's mean z (deviation
				// energy = persistence × magnitude) exceeds the significance k.
				spaceScore = sum / float64(len(zscores))
				spaceAbnormal = spaceScore > cfg.SpaceClusterK
			default:
				spaceScore = sum / float64(len(zscores))
				spaceAbnormal = spaceScore > cfg.SpaceZThreshold
			}

			result[cid][metric] = &MetricAnomalyDetail{
				Metric:        metric,
				SpaceScore:    spaceScore,
				SpaceAbnormal: spaceAbnormal,
			}
		}
	}

	return result
}
