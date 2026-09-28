package detector

import (
	"testing"

	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/config"
)

func resetSlowCommConfig() {
	config.SlowCommRatio = 1.3
	config.SlowCommMinCount = 1000
}

// TestParseBandwidthCol verifies dynamic column-name parsing.
func TestParseBandwidthCol(t *testing.T) {
	prefix := "tp_"
	cases := []struct {
		col       string
		wantOp    string
		wantCount int
		wantOK    bool
	}{
		{"tp_ReduceScatter_8192", "ReduceScatter", 8192, true},
		{"tp_allReduce_20480", "allReduce", 20480, true},
		{"tp_Duration", "", 0, false},
		{"tp_Count", "", 0, false},
		{"tp_foo", "", 0, false},
		{"dp_allGather_4096", "", 0, false},
	}
	for _, c := range cases {
		op, cnt, ok := parseBandwidthCol(prefix, c.col)
		if ok != c.wantOK || op != c.wantOp || cnt != c.wantCount {
			t.Errorf("parseBandwidthCol(%q) = (%q,%d,%v), want (%q,%d,%v)",
				c.col, op, cnt, ok, c.wantOp, c.wantCount, c.wantOK)
		}
	}
}

// TestDetectSlowDomainByBandwidth flags the low-bandwidth group via kmeans.
func TestDetectSlowDomainByBandwidth(t *testing.T) {
	resetSlowCommConfig()
	parallels := map[string][][]int{"tp": {{0, 1}, {2, 3}}}
	stepData := map[string]map[int]float64{
		"tp_allReduce_20480": {0: 5.0, 1: 5.0, 2: 20.0, 3: 20.0},
	}
	res := config.NewDegradationData()
	DetectSlowDomainByBandwidth(parallels, stepData, res)

	comm := res["comm"]
	if len(comm) != 1 {
		t.Fatalf("comm = %v, want 1 entry", comm)
	}
	if v, ok := comm["0,1"]; !ok {
		t.Errorf("expected group 0,1, got %v", comm)
	} else if v < 1.0 {
		t.Errorf("degradation should be >1, got %v", v)
	}
}

// TestDetectSlowDomainMaxCountRepresentative picks each group's largest-count
// entry as its bandwidth representative (ignoring a small-count anomaly).
func TestDetectSlowDomainMaxCountRepresentative(t *testing.T) {
	resetSlowCommConfig()
	parallels := map[string][][]int{"tp": {{0, 1}, {2, 3}}}
	stepData := map[string]map[int]float64{
		"tp_allReduce_16384": {0: 5.0, 1: 5.0, 2: 100.0, 3: 100.0}, // slow on small count
		"tp_allReduce_32768": {0: 100.0, 1: 100.0, 2: 100.0, 3: 100.0},
	}
	res := config.NewDegradationData()
	DetectSlowDomainByBandwidth(parallels, stepData, res)
	if got := res["comm"]; len(got) != 0 {
		t.Errorf("expected no slow (representative = max count 32768), got %v", got)
	}
}

// TestDetectSlowDomainHalfRangeFilter drops groups whose representative count
// is below 50% of the largest count.
func TestDetectSlowDomainHalfRangeFilter(t *testing.T) {
	resetSlowCommConfig()
	parallels := map[string][][]int{"tp": {{0, 1}, {2, 3}, {4, 5}}}
	stepData := map[string]map[int]float64{
		"tp_allReduce_40000": {0: 20, 1: 20, 2: 100, 3: 100},
		"tp_allReduce_15000": {4: 1, 5: 1}, // 15000 < 40000*0.5 → filtered out
	}
	res := config.NewDegradationData()
	DetectSlowDomainByBandwidth(parallels, stepData, res)

	comm := res["comm"]
	if _, ok := comm["0,1"]; !ok {
		t.Fatalf("expected group 0,1, got %v", comm)
	}
	if _, ok := comm["4,5"]; ok {
		t.Errorf("group 4,5 should be filtered out, got %v", comm)
	}
}

// TestDetectSlowDomainCountFloor drops groups whose count is <= 10240 even if
// they pass the -50% window.
func TestDetectSlowDomainCountFloor(t *testing.T) {
	resetSlowCommConfig()
	parallels := map[string][][]int{"tp": {{0, 1}, {2, 3}, {4, 5}}}
	stepData := map[string]map[int]float64{
		"tp_allReduce_15000": {0: 20, 1: 20, 2: 100, 3: 100},
		"tp_allReduce_8000":  {4: 1, 5: 1}, // 8000 >= 7500 (-50% ok) but <= 10240 (floor)
	}
	res := config.NewDegradationData()
	DetectSlowDomainByBandwidth(parallels, stepData, res)

	comm := res["comm"]
	if _, ok := comm["0,1"]; !ok {
		t.Fatalf("expected group 0,1, got %v", comm)
	}
	if _, ok := comm["4,5"]; ok {
		t.Errorf("group 4,5 (count 8000) should be filtered out by floor, got %v", comm)
	}
}

// TestDetectSlowDomainAllOpsAnomalous reports a group only when it is anomalous
// on EVERY opType of the domain.
func TestDetectSlowDomainAllOpsAnomalous(t *testing.T) {
	resetSlowCommConfig()
	parallels := map[string][][]int{"tp": {{0, 1}, {2, 3}}}
	stepData := map[string]map[int]float64{
		"tp_allReduce_20480": {0: 5, 1: 5, 2: 20, 3: 20},
		"tp_allGather_20480": {0: 5, 1: 5, 2: 20, 3: 20},
	}
	res := config.NewDegradationData()
	DetectSlowDomainByBandwidth(parallels, stepData, res)
	if _, ok := res["comm"]["0,1"]; !ok {
		t.Errorf("expected group 0,1 (slow on all ops), got %v", res["comm"])
	}
}

// TestDetectSlowDomainPartialOpsNotReported does NOT report a group that is
// anomalous on only some opTypes.
func TestDetectSlowDomainPartialOpsNotReported(t *testing.T) {
	resetSlowCommConfig()
	parallels := map[string][][]int{"tp": {{0, 1}, {2, 3}}}
	stepData := map[string]map[int]float64{
		"tp_allReduce_20480": {0: 5, 1: 5, 2: 20, 3: 20},
		"tp_allGather_20480": {0: 20, 1: 20, 2: 20, 3: 20},
	}
	res := config.NewDegradationData()
	DetectSlowDomainByBandwidth(parallels, stepData, res)
	if got := res["comm"]; len(got) != 0 {
		t.Errorf("expected no report (group 0 not slow on all ops), got %v", got)
	}
}

// TestDetectSlowDomainReportsMultipleGroups can report more than one group when
// multiple groups are anomalous on every opType.
func TestDetectSlowDomainReportsMultipleGroups(t *testing.T) {
	resetSlowCommConfig()
	parallels := map[string][][]int{"tp": {{0, 1}, {2, 3}, {4, 5}}}
	stepData := map[string]map[int]float64{
		"tp_allReduce_20480": {0: 5, 1: 5, 2: 20, 3: 20, 4: 5, 5: 5},
	}
	res := config.NewDegradationData()
	DetectSlowDomainByBandwidth(parallels, stepData, res)

	comm := res["comm"]
	if _, ok := comm["0,1"]; !ok {
		t.Errorf("expected group 0,1, got %v", comm)
	}
	if _, ok := comm["4,5"]; !ok {
		t.Errorf("expected group 4,5, got %v", comm)
	}
}

// TestDetectSlowDomainByBandwidthNoSlow leaves near-equal bandwidth un-flagged.
func TestDetectSlowDomainByBandwidthNoSlow(t *testing.T) {
	resetSlowCommConfig()
	parallels := map[string][][]int{"tp": {{0, 1}, {2, 3}}}
	stepData := map[string]map[int]float64{
		"tp_allReduce_20480": {0: 100, 1: 100, 2: 101, 3: 101},
	}
	res := config.NewDegradationData()
	DetectSlowDomainByBandwidth(parallels, stepData, res)
	if got := res["comm"]; len(got) != 0 {
		t.Errorf("expected no slow, got %v", got)
	}
}

// TestDetectSlowDomainByBandwidthSkipsPP skips the point-to-point pp domain.
func TestDetectSlowDomainByBandwidthSkipsPP(t *testing.T) {
	resetSlowCommConfig()
	parallels := map[string][][]int{"pp": {{0, 1}, {2, 3}}}
	stepData := map[string]map[int]float64{
		"pp_allReduce_20480": {0: 5, 1: 5, 2: 20, 3: 20},
	}
	res := config.NewDegradationData()
	DetectSlowDomainByBandwidth(parallels, stepData, res)
	if got := res["comm"]; len(got) != 0 {
		t.Errorf("pp should be skipped, got %v", got)
	}
}