package detector

import (
	"strconv"
	"strings"

	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/clustering"
	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/config"
)

// ---------------------------------------------------------------------------
// Slow-domain detection by bandwidth clustering
//
// The dataparse backfill pass writes per-(opType,count) bandwidths into the CSV
// as dynamic columns "<domain>_<opType>_<count>". For each collective parallel
// domain and each opType, we:
//  1. take, per group, the entry with the largest count as its representative
//     (a larger count reflects the true bandwidth better);
//  2. take the max of those representative counts and keep only the groups
//     whose count is within -50% of that max (count >= max*0.5);
//  3. cluster the remaining representative bandwidths with the shared kmeans
//     recursive detector (min direction: lower bandwidth is slower), using
//     SlowCommRatio as the threshold (default 1.3);
//  4. a group is reported only when it is anomalous on EVERY opType of the
//     domain; its reported degradation is the largest across those opTypes.
// ---------------------------------------------------------------------------

// bwEntry is one group's (opType × count) bandwidth.
type bwEntry struct {
	opType string
	count  int
	bw     float64
}

// slowCommCountFloor is the absolute lower bound on a group's representative
// count: below this the count is treated as noise even within the -50% window.
const slowCommCountFloor = 10240

// DetectSlowDomainByBandwidth flags slow communication groups per collective
// parallel domain using the shared kmeans detector. A group must be anomalous
// on every opType to be reported (reusing comm_domain_result).
func DetectSlowDomainByBandwidth(parallels map[string][][]int, stepData map[string]map[int]float64, localResult config.DegradationData) {
	ratio := config.SlowCommRatio
	if ratio <= 0 {
		ratio = 1.3
	}

	for domain, groups := range parallels {
		if domain == ppParallelDomainName || domain == "embd" {
			continue
		}
		if len(groups) < 2 {
			continue
		}

		// Per-group bandwidth entries (each group may miss some combos).
		groupBWs := make([][]bwEntry, len(groups))
		for i, group := range groups {
			groupBWs[i] = bwSetForGroup(domain, group, stepData)
		}

		opTypes := collectOpTypes(groupBWs)
		if len(opTypes) == 0 {
			continue
		}

		// anomalous[groupIdx][opType] = degradation (>1), only for flagged
		// (group, opType) pairs.
		anomalous := make(map[int]map[string]float64)

		for opType := range opTypes {
			// Representative per group: the entry with the largest count.
			type rep struct {
				groupIdx int
				count    int
				bw       float64
			}
			var reps []rep
			for gi, bws := range groupBWs {
				maxC := -1
				var maxBW float64
				for _, e := range bws {
					if e.opType != opType || e.count <= maxC {
						continue
					}
					maxC = e.count
					maxBW = e.bw
				}
				if maxC >= 0 {
					reps = append(reps, rep{groupIdx: gi, count: maxC, bw: maxBW})
				}
			}
			if len(reps) < 2 {
				continue
			}

			// Keep only groups within -50% of the largest representative count
			// AND whose count exceeds the floor (small counts are noise).
			maxCount := 0
			for _, r := range reps {
				if r.count > maxCount {
					maxCount = r.count
				}
			}
			half := float64(maxCount) * 0.5

			var kept []rep
			for _, r := range reps {
				if float64(r.count) >= half && r.count > slowCommCountFloor {
					kept = append(kept, r)
				}
			}
			if len(kept) < 2 {
				continue
			}

			// Cluster the remaining representative bandwidths (min direction).
			bws := make([]float64, len(kept))
			for i, r := range kept {
				bws[i] = r.bw
			}
			for _, r := range clustering.Detect(bws, ratio, false) {
				gi := kept[r.Index].groupIdx
				deg := 1.0 / r.Ratio // baseline/value → >1, larger = slower
				if anomalous[gi] == nil {
					anomalous[gi] = map[string]float64{}
				}
				anomalous[gi][opType] = deg
			}
		}

		// Report only groups anomalous on EVERY opType of the domain, with the
		// largest degradation across those opTypes.
		for gi, opDegs := range anomalous {
			if len(opDegs) != len(opTypes) {
				continue
			}
			maxDeg := 0.0
			for _, deg := range opDegs {
				if deg > maxDeg {
					maxDeg = deg
				}
			}
			localResult.AddGroup("comm", groups[gi], maxDeg)
		}
	}
}

// collectOpTypes returns the distinct op types across all groups of a domain.
func collectOpTypes(groupBWs [][]bwEntry) map[string]bool {
	s := make(map[string]bool)
	for _, bws := range groupBWs {
		for _, e := range bws {
			s[e.opType] = true
		}
	}
	return s
}

// bwSetForGroup collects the bandwidth for every (opType,count) column of one
// domain group. All ranks of a group share the same backfilled bandwidth, so
// the first rank that has the value is used as the representative.
func bwSetForGroup(domain string, group []int, stepData map[string]map[int]float64) []bwEntry {
	prefix := domain + "_"
	var out []bwEntry
	for col, byRank := range stepData {
		opType, count, ok := parseBandwidthCol(prefix, col)
		if !ok {
			continue
		}
		var v float64
		found := false
		for _, r := range group {
			if val, ok := byRank[r]; ok {
				v, found = val, true
				break
			}
		}
		if !found {
			continue
		}
		out = append(out, bwEntry{opType: opType, count: count, bw: v})
	}
	return out
}

// parseBandwidthCol parses a bandwidth column name "<opType>_<count>" (given
// the domain prefix, e.g. "tp_") into its op type and count. It returns false
// for the diagnostic Duration/Count columns and any non-numeric tail.
func parseBandwidthCol(prefix, col string) (string, int, bool) {
	if !strings.HasPrefix(col, prefix) {
		return "", 0, false
	}
	rest := strings.TrimPrefix(col, prefix)
	rest = strings.TrimPrefix(rest, "_")
	idx := strings.LastIndex(rest, "_")
	if idx <= 0 || idx == len(rest)-1 {
		return "", 0, false
	}
	opType := rest[:idx]
	countStr := rest[idx+1:]
	count, err := strconv.Atoi(countStr)
	if err != nil {
		return "", 0, false
	}
	if opType == "Duration" || opType == "Count" {
		return "", 0, false
	}
	return opType, count, true
}
