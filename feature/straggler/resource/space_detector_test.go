package resource

import (
	"testing"
)

// =============================================================================
// detectSpaceAnomalies — MethodCluster (aicore_freq) kmeans ratio tests
// =============================================================================

// freqRows builds one detection row with the given per-card aicore_freq.
// Cards absent from the map are absent from the row entirely.
func freqRows(freqs map[int]float64) []CSVRow {
	row := CSVRow{Timestamp: 1_000_000, AICoreFreq: freqs}
	return []CSVRow{row}
}

func freqCardIDs(n int) []int {
	ids := make([]int, n)
	for i := range ids {
		ids[i] = i
	}
	return ids
}

// Severe single downclock must be flagged: 800MHz vs 1800MHz peers → ratio
// 1800/800 = 2.25 > SpaceRatioThreshold (2.0). aicore_freq now uses the same
// kmeans ratio detection as the other cluster metrics.
func TestSpaceFreqSingleDownclock(t *testing.T) {
	cfg := DefaultDetectionConfig()
	cardIDs := freqCardIDs(8)
	freqs := map[int]float64{0: 1800, 1: 1800, 2: 1800, 3: 1800, 4: 1800, 5: 1800, 6: 1800, 7: 800}

	res := detectSpaceAnomalies(freqRows(freqs), cardIDs, cfg)
	if got := res.Scores[7][MetricAICoreFreq][0]; got < 2.24 || got > 2.26 {
		t.Fatalf("downclocked card 7 → space score = %v, want ≈2.25", got)
	}
	for cid := 0; cid < 7; cid++ {
		if z := res.Scores[cid][MetricAICoreFreq][0]; z != 1.0 {
			t.Errorf("normal card %d → space score = %v, want 1.0 (neutral ratio)", cid, z)
		}
	}

	details := aggregateScores(res, cardIDs, cfg)
	if d := details[7][MetricAICoreFreq]; !d.Abnormal {
		t.Errorf("card 7 spaceAbnormal = false, want true (score=%v)", d.Score)
	}
	if d := details[0][MetricAICoreFreq]; d.Abnormal {
		t.Errorf("card 0 spaceAbnormal = true, want false")
	}
}

// All cards on the same clock level → nobody flagged.
func TestSpaceFreqAllNormal(t *testing.T) {
	cfg := DefaultDetectionConfig()
	cardIDs := freqCardIDs(4)
	freqs := map[int]float64{0: 1800, 1: 1800, 2: 1800, 3: 1800}

	res := detectSpaceAnomalies(freqRows(freqs), cardIDs, cfg)
	for _, cid := range cardIDs {
		if z := res.Scores[cid][MetricAICoreFreq][0]; z != 1.0 {
			t.Errorf("card %d at common clock → score = %v, want 1.0 (neutral ratio)", cid, z)
		}
	}
	details := aggregateScores(res, cardIDs, cfg)
	for _, cid := range cardIDs {
		if d := details[cid][MetricAICoreFreq]; d.Score != 1.0 {
			t.Errorf("card %d aggregate space score = %v, want 1.0", cid, d.Score)
		}
	}
}

// Two cards downclocked to the SAME value (800 vs 1800 peers, ratio 2.25) are
// BOTH flagged — the multi-card case the old peer-min direct method missed.
func TestSpaceFreqMultiDownclock(t *testing.T) {
	cfg := DefaultDetectionConfig()
	cardIDs := freqCardIDs(8)
	freqs := map[int]float64{0: 1800, 1: 1800, 2: 1800, 3: 1800, 4: 1800, 5: 1800, 6: 800, 7: 800}

	res := detectSpaceAnomalies(freqRows(freqs), cardIDs, cfg)
	details := aggregateScores(res, cardIDs, cfg)
	for _, cid := range []int{6, 7} {
		if !details[cid][MetricAICoreFreq].Abnormal {
			t.Errorf("downclocked card %d should be space-abnormal (score=%v)",
				cid, details[cid][MetricAICoreFreq].Score)
		}
	}
	for cid := 0; cid < 6; cid++ {
		if details[cid][MetricAICoreFreq].Abnormal {
			t.Errorf("normal card %d should not be space-abnormal", cid)
		}
	}
}

// A card absent from the row must not be flagged.
func TestSpaceFreqAbsentCardNotFlagged(t *testing.T) {
	cfg := DefaultDetectionConfig()
	cardIDs := freqCardIDs(4)
	freqs := map[int]float64{0: 1800, 1: 1800, 2: 1800} // card 3 absent

	res := detectSpaceAnomalies(freqRows(freqs), cardIDs, cfg)
	if z := res.Scores[3][MetricAICoreFreq][0]; z != 0 {
		t.Errorf("absent card 3 → score = %v, want 0", z)
	}
}

// A card at 0 MHz among 1800 MHz peers: the 0 is clamped to zeroFloor (not
// dropped) and participates — min run flags 1 (the zero card), max run flags
// 7 (the working cards above the floor) → minority = the zero card, reported
// with the unified cluster/baseline ratio ≈ zeroFloor/1800 (far below the
// 0.5 anomaly edge).
func TestSpaceFreqZeroDownclock(t *testing.T) {
	cfg := DefaultDetectionConfig()
	cardIDs := freqCardIDs(8)
	freqs := map[int]float64{0: 1800, 1: 1800, 2: 1800, 3: 1800, 4: 1800, 5: 1800, 6: 1800, 7: 0}

	res := detectSpaceAnomalies(freqRows(freqs), cardIDs, cfg)
	details := aggregateScores(res, cardIDs, cfg)

	if !details[7][MetricAICoreFreq].Abnormal {
		t.Errorf("zero-freq card 7 should be space-abnormal (score=%v)",
			details[7][MetricAICoreFreq].Score)
	}
	for cid := 0; cid < 7; cid++ {
		if details[cid][MetricAICoreFreq].Abnormal {
			t.Errorf("1800MHz card %d should not be space-abnormal", cid)
		}
	}
}

// aicore_util: 7 cards at 0% vs 1 card at 100% — zeros participate (clamped),
// max run flags 1 (the busy card), min run flags 7 (the idle cards above the
// floor) → minority = the busy card, reported with a very large ratio.
func TestSpaceUtilZeroPeers(t *testing.T) {
	cfg := DefaultDetectionConfig()
	cardIDs := clusterCardIDs(8)

	rows := []CSVRow{{
		Timestamp:  1_000_000,
		AICoreUtil: map[int]float64{0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0, 7: 100},
	}}
	res := detectSpaceAnomalies(rows, cardIDs, cfg)
	details := aggregateScores(res, cardIDs, cfg)

	if !details[7][MetricAICoreUtil].Abnormal {
		t.Errorf("busy card 7 should be space-abnormal (score=%v)",
			details[7][MetricAICoreUtil].Score)
	}
	if got := details[7][MetricAICoreUtil].Score; got < 100 {
		t.Errorf("busy card 7 score = %v, want ≥100 (far above the zero floor)", got)
	}
	for cid := 0; cid < 7; cid++ {
		if details[cid][MetricAICoreUtil].Abnormal {
			t.Errorf("idle card %d should not be space-abnormal", cid)
		}
	}
}

// A mild downclock (1500 vs 1800, ratio 1.2 < 2.0) is NOT space-flagged: the
// global ratio threshold keeps space for severe (>2×) drops, while the time
// dimension (freq MAD Z-score vs own history) owns the mild ones.
func TestSpaceFreqMildDownclock(t *testing.T) {
	cfg := DefaultDetectionConfig()
	cardIDs := freqCardIDs(4)
	freqs := map[int]float64{0: 1800, 1: 1800, 2: 1800, 3: 1500}

	res := detectSpaceAnomalies(freqRows(freqs), cardIDs, cfg)
	details := aggregateScores(res, cardIDs, cfg)
	if details[3][MetricAICoreFreq].Abnormal {
		t.Errorf("mild downclock card 3 should not be space-abnormal (score=%v)",
			details[3][MetricAICoreFreq].Score)
	}
}

// =============================================================================
// detectSpaceAnomalies — MethodCluster (kmeans ratio) tests
// =============================================================================

// clusterTempRows builds detection rows from per-row temp patterns (one slice
// per row, aligned with cardIDs 0..n-1).
func clusterTempRows(patterns [][]float64) []CSVRow {
	var rows []CSVRow
	ts := int64(1_000_000)
	for _, pat := range patterns {
		m := make(map[int]float64, len(pat))
		for i, v := range pat {
			m[i] = v
		}
		rows = append(rows, CSVRow{Timestamp: ts, Temp: m})
		ts += 60
	}
	return rows
}

func clusterCardIDs(n int) []int {
	return freqCardIDs(n)
}

// Single anomalous card (100°C vs 30°C peers, ratio 3.33 > 2.0) must be flagged.
func TestSpaceClusterSingleAnomaly(t *testing.T) {
	cfg := DefaultDetectionConfig() // SpaceRatioThreshold = 2.0
	cardIDs := clusterCardIDs(8)

	rows := clusterTempRows([][]float64{{30, 30, 30, 30, 30, 30, 30, 100}})
	res := detectSpaceAnomalies(rows, cardIDs, cfg)
	details := aggregateScores(res, cardIDs, cfg)

	if !details[7][MetricTemp].Abnormal {
		t.Errorf("hot card 7 should be space-abnormal (score=%v)", details[7][MetricTemp].Score)
	}
	if got := details[7][MetricTemp].Score; got < 3.33 || got > 3.34 {
		t.Errorf("hot card 7 score = %v, want ≈3.33", got)
	}
	for cid := 0; cid < 7; cid++ {
		if details[cid][MetricTemp].Abnormal {
			t.Errorf("normal card %d should not be space-abnormal", cid)
		}
	}
}

// Two anomalous cards (both 100°C) must BOTH be flagged — the multi-card case
// that the old mean/std z-score diluted.
func TestSpaceClusterMultiAnomaly(t *testing.T) {
	cfg := DefaultDetectionConfig()
	cardIDs := clusterCardIDs(8)

	rows := clusterTempRows([][]float64{{30, 30, 30, 30, 30, 30, 100, 100}})
	res := detectSpaceAnomalies(rows, cardIDs, cfg)
	details := aggregateScores(res, cardIDs, cfg)

	for _, cid := range []int{6, 7} {
		if !details[cid][MetricTemp].Abnormal {
			t.Errorf("hot card %d should be space-abnormal (score=%v)", cid, details[cid][MetricTemp].Score)
		}
	}
	for cid := 0; cid < 6; cid++ {
		if details[cid][MetricTemp].Abnormal {
			t.Errorf("normal card %d should not be space-abnormal", cid)
		}
	}
}

// A spread fleet (54..60, max ratio 60/54 ≈ 1.11 < 2.0) has no cluster whose
// mean ratio exceeds the threshold → nobody flagged. Natural spread must not
// be treated as an anomaly.
func TestSpaceClusterMajorityNormalSpread(t *testing.T) {
	cfg := DefaultDetectionConfig()
	cardIDs := clusterCardIDs(8)

	rows := clusterTempRows([][]float64{{54, 55, 55, 56, 57, 58, 59, 60}})
	res := detectSpaceAnomalies(rows, cardIDs, cfg)
	details := aggregateScores(res, cardIDs, cfg)

	for _, cid := range cardIDs {
		if details[cid][MetricTemp].Abnormal {
			t.Errorf("card %d in a normal spread should not be space-abnormal (score=%v)",
				cid, details[cid][MetricTemp].Score)
		}
	}
}

// A mild fleet-wide shift (60 vs 55, ratio 1.09 < 2.0) stays silent even when
// the higher group is the majority — the ratio threshold, not majority
// membership, decides.
func TestSpaceClusterMajorityAnomaly(t *testing.T) {
	cfg := DefaultDetectionConfig()
	cardIDs := clusterCardIDs(8)

	rows := clusterTempRows([][]float64{{60, 60, 60, 60, 60, 55, 55, 55}})
	res := detectSpaceAnomalies(rows, cardIDs, cfg)
	details := aggregateScores(res, cardIDs, cfg)

	for _, cid := range cardIDs {
		if details[cid][MetricTemp].Abnormal {
			t.Errorf("card %d: mild fleet-wide shift must stay silent (score=%v)",
				cid, details[cid][MetricTemp].Score)
		}
	}
}

// A 4/4 tie (4 cards at 30°C vs 4 at 80°C, ratio 2.67 > 2.0) yields equal
// flagged counts in BOTH directions (α1 = α2 = 4) → no minority can be called
// → nobody is reported, per the equal-counts rule.
func TestSpaceClusterTieEqualCountsNoReport(t *testing.T) {
	cfg := DefaultDetectionConfig()
	cardIDs := clusterCardIDs(8)

	rows := clusterTempRows([][]float64{{30, 30, 30, 30, 80, 80, 80, 80}})
	res := detectSpaceAnomalies(rows, cardIDs, cfg)
	details := aggregateScores(res, cardIDs, cfg)

	for _, cid := range cardIDs {
		if details[cid][MetricTemp].Abnormal {
			t.Errorf("card %d in a 4/4 tie should NOT be space-abnormal (score=%v)",
				cid, details[cid][MetricTemp].Score)
		}
	}
}

// Direction-agnostic both ways: for temp (traditionally a high-is-anomaly
// metric), a pair of COLD cards (10°C vs 60°C peers, ratio 6.0) are the
// minority flags — max run flags the 6 warm cards above the 10°C baseline,
// min run flags the 2 cold cards below the 60°C baseline → the 2 cold cards
// are reported.
func TestSpaceClusterColdMinority(t *testing.T) {
	cfg := DefaultDetectionConfig()
	cardIDs := clusterCardIDs(8)

	rows := clusterTempRows([][]float64{{60, 60, 60, 60, 60, 60, 10, 10}})
	res := detectSpaceAnomalies(rows, cardIDs, cfg)
	details := aggregateScores(res, cardIDs, cfg)

	for _, cid := range []int{6, 7} {
		if !details[cid][MetricTemp].Abnormal {
			t.Errorf("cold card %d should be space-abnormal (score=%v)",
				cid, details[cid][MetricTemp].Score)
		}
	}
	for cid := 0; cid < 6; cid++ {
		if details[cid][MetricTemp].Abnormal {
			t.Errorf("warm card %d should not be space-abnormal", cid)
		}
	}
}

// Direction-agnostic low case (aicore_util): 6 cards working at 90% vs 2
// cards idle at 30% (ratio 3.0 > 2.0). Max run flags the 6 working cards
// above the 30% baseline, min run flags the 2 idle cards below the 90%
// baseline → minority = the 2 idle cards are reported.
func TestSpaceClusterLowMinority(t *testing.T) {
	cfg := DefaultDetectionConfig()
	cardIDs := clusterCardIDs(8)

	// 6 cards working at 90%, 2 cards at 30% (util dropped, ratio 3.0 > 2.0).
	rows := []CSVRow{{
		Timestamp:  1_000_000,
		AICoreUtil: map[int]float64{0: 90, 1: 90, 2: 90, 3: 90, 4: 90, 5: 90, 6: 30, 7: 30},
	}}
	res := detectSpaceAnomalies(rows, cardIDs, cfg)
	details := aggregateScores(res, cardIDs, cfg)

	for _, cid := range []int{6, 7} {
		if !details[cid][MetricAICoreUtil].Abnormal {
			t.Errorf("low-util card %d should be space-abnormal (score=%v)",
				cid, details[cid][MetricAICoreUtil].Score)
		}
	}
	for cid := 0; cid < 6; cid++ {
		if details[cid][MetricAICoreUtil].Abnormal {
			t.Errorf("working card %d should not be space-abnormal", cid)
		}
	}
}

// Space detection judges ONLY the last aggregated point: an anomaly in
// an earlier row (but a clean last row) is not flagged, and a clean earlier
// row followed by an anomalous last row IS flagged.
func TestSpaceClusterLastPointOnly(t *testing.T) {
	cfg := DefaultDetectionConfig()
	cardIDs := clusterCardIDs(8)

	// Anomaly in the first row only → last row is clean → nothing flagged.
	rowsCleanLast := clusterTempRows([][]float64{
		{30, 30, 30, 30, 30, 30, 30, 100},
		{30, 30, 30, 30, 30, 30, 30, 30},
	})
	res := detectSpaceAnomalies(rowsCleanLast, cardIDs, cfg)
	details := aggregateScores(res, cardIDs, cfg)
	if details[7][MetricTemp].Abnormal {
		t.Errorf("card 7 flagged although the LAST point was normal")
	}

	// Clean first row, anomalous last row → flagged.
	rowsLast := clusterTempRows([][]float64{
		{30, 30, 30, 30, 30, 30, 30, 30},
		{30, 30, 30, 30, 30, 30, 30, 100},
	})
	resLast := detectSpaceAnomalies(rowsLast, cardIDs, cfg)
	detailsLast := aggregateScores(resLast, cardIDs, cfg)
	if !detailsLast[7][MetricTemp].Abnormal {
		t.Errorf("card 7 should be flagged on the last point (score=%v)",
			detailsLast[7][MetricTemp].Score)
	}
	if got := len(resLast.Scores[7][MetricTemp]); got != 1 {
		t.Errorf("score array has %d elements, want exactly 1 (last point only)", got)
	}
}
