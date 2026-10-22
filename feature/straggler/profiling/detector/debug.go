package detector

import (
	"sort"

	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/clustering"
	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/config"
)

// DebugRankScores computes, for every valid rank, its per-category diagnostic
// score so that --debug-output can show NORMAL ranks' values alongside flagged
// ones:
//   - cal:        top-level kmeans ratio of ZP_Kernel (max) — a normal rank
//     sits near 1.0
//   - cpu:        top-level kmeans ratio of ZP_Host (max)
//   - npu_bubble: raw ZP_Bubble value
//
// Ranks absent from a metric simply lack that key (no data to diagnose).
func DebugRankScores(stepData map[string]map[int]float64, validRanks []int) map[int]map[string]float64 {
	scores := make(map[int]map[string]float64, len(validRanks))
	for _, r := range validRanks {
		scores[r] = map[string]float64{}
	}
	set := func(r int, cat string, v float64) {
		if s, ok := scores[r]; ok {
			s[cat] = v
		}
	}

	if kernel, ok := stepData[zpKernelColumn]; ok {
		for r, ratio := range rankRatios(kernel, config.CalThreshold, true) {
			set(r, "cal", ratio)
		}
	}
	if host, ok := stepData[zpHostDataColumn]; ok {
		for r, ratio := range rankRatios(host, config.CPUThreshold, true) {
			set(r, "cpu", ratio)
		}
	}
	if bubble, ok := stepData[zpBubble]; ok {
		for r, v := range bubble {
			set(r, "npu_bubble", v)
		}
	}
	return scores
}

// rankRatios computes each rank's top-level kmeans ratio for one metric (sorted
// by rank for determinism). Uses the same single-level diagnostic as the debug
// view, not the recursive decision.
func rankRatios(data map[int]float64, threshold float64, highIsAnomaly bool) map[int]float64 {
	ranks := make([]int, 0, len(data))
	for r := range data {
		ranks = append(ranks, r)
	}
	sort.Ints(ranks)
	vals := make([]float64, len(ranks))
	for i, r := range ranks {
		vals[i] = data[r]
	}
	entries := clustering.Diagnose(vals, threshold, highIsAnomaly)
	out := make(map[int]float64, len(entries))
	for _, e := range entries {
		out[ranks[e.Index]] = e.Ratio
	}
	return out
}
