package detector

import (
	"sort"

	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/config"
)

// ---------------------------------------------------------------------------
// Slow compute detection
// ---------------------------------------------------------------------------

// getSlowCalculateRanks detects compute-straggler ranks by testing each group
// of the primary detection domain.
func getSlowCalculateRanks(detectionGroups [][]int, alignedData map[string]map[int]float64, detectionParallel string, localResult config.DegradationData) error {
	for _, npuGroup := range detectionGroups {
		abnormalRanks, degradations := detCalForOneGroup(alignedData, npuGroup)
		for i, rank := range abnormalRanks {
			localResult.AddSingle("cal", rank, degradations[i])
		}
	}
	return nil
}

// detCalForOneGroup runs homogeneous clustering on a single compute group
// using only ZP_Kernel (direction "max"). When any rank in the group is
// missing ZP_Kernel (<= 0), the group is skipped — there is no fallback metric.
func detCalForOneGroup(alignedData map[string]map[int]float64, npuGroup []int) ([]int, []float64) {
	if len(npuGroup) < minRanksInGroup {
		return nil, nil
	}

	kernelMap, ok := alignedData[zpKernelColumn]
	if !ok {
		return nil, nil
	}

	// Build data arrays aligned by rank; require ZP_Kernel for ALL ranks.
	ranks := make([]int, 0, len(npuGroup))
	values := make([]float64, 0, len(npuGroup))
	for _, npuID := range npuGroup {
		v, ok := kernelMap[npuID]
		if !ok || v <= 0 {
			return nil, nil
		}
		ranks = append(ranks, npuID)
		values = append(values, v)
	}

	if len(ranks) < minRanksInGroup {
		return nil, nil
	}

	return HomogenizationComparisonFunc(ranks, values, config.CalThreshold, "max")
}

// ---------------------------------------------------------------------------
// NPU Bubble detection
// ---------------------------------------------------------------------------

// detectionZpBubbleData applies a fixed threshold (< 5000 ns) to flag ranks
// with insufficient NPU idle time.
func detectionZpBubbleData(npuData map[int]float64, localResult config.DegradationData) {
	const bubbleThreshold = 5000.0 // 5 µs

	for npuID, value := range npuData {
		if value <= 0 {
			continue
		}
		if value < bubbleThreshold {
			localResult.AddSingle("npu_bubble", npuID, value)
		}
	}
}

// ---------------------------------------------------------------------------
// Slow CPU detection
// ---------------------------------------------------------------------------

// getSlowHostRanksByHomogenize detects CPU-straggler ranks using ZP_Host data
// preprocessed with host-based trimmed means (cards on the same physical node
// are grouped by hostUid, intra-node values are smoothed via trimmed mean).
func getSlowHostRanksByHomogenize(npus []int, detectionData map[int]float64, localResult config.DegradationData, rankToHostUid map[int]string) []int {
	var haveDataRanks []int
	var ranksData []float64

	for _, npuID := range npus {
		if v, ok := detectionData[npuID]; ok {
			haveDataRanks = append(haveDataRanks, npuID)
			ranksData = append(ranksData, v)
		}
	}

	if len(haveDataRanks) < minRanksInGroup {
		return nil
	}

	// Preprocess: group by hostUid, trimmed mean per host.
	smoothByHostUid(ranksData, haveDataRanks, rankToHostUid)

	abnormalRanks, degradations := HomogenizationComparisonFunc(haveDataRanks, ranksData, config.CPUThreshold, "max")
	for i, rank := range abnormalRanks {
		localResult.AddSingle("cpu", rank, degradations[i])
	}
	return abnormalRanks
}

// smoothByHostUid groups cards by their hostUid (same physical machine) and
// replaces each card's ZP_Host value with the trimmed mean of its host group.
// Cards without a hostUid mapping keep their original values unchanged.
// The trimmed mean discards the min and max value within the group.
func smoothByHostUid(ranksData []float64, ranks []int, rankToHostUid map[int]string) {
	// Group indices by hostUid.
	hostGroups := make(map[string][]int)
	for i, rank := range ranks {
		uid, ok := rankToHostUid[rank]
		if !ok || uid == "" {
			continue // no hostUid: leave value unchanged
		}
		hostGroups[uid] = append(hostGroups[uid], i)
	}

	for _, indices := range hostGroups {
		if len(indices) <= 1 {
			continue // single card on this host: no peer smoothing needed
		}

		vals := make([]float64, len(indices))
		for i, idx := range indices {
			vals[i] = ranksData[idx]
		}

		var mean float64
		if len(vals) > 2 {
			sorted := make([]float64, len(vals))
			copy(sorted, vals)
			sort.Float64s(sorted)
			trimmed := sorted[1 : len(sorted)-1]
			var sum float64
			for _, v := range trimmed {
				sum += v
			}
			mean = sum / float64(len(trimmed))
		} else {
			var sum float64
			for _, v := range vals {
				sum += v
			}
			mean = sum / float64(len(vals))
		}

		for _, idx := range indices {
			ranksData[idx] = mean
		}
	}
}
