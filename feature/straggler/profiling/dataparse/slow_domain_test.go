package dataparse

import (
	"math"
	"testing"
)

func TestPureCommKind(t *testing.T) {
	cases := []struct {
		name string
		want string
	}{
		{"hcom_allGather_AicpuKernel_503_483_1", "allgather"},
		{"HcclAllGather", "allgather"},
		{"hcom_allgather_v2_2_1", "allgather"},
		{"HcclAllGatherv", "allgatherv"},
		{"HcclAllGatherBase", "allgatherbase"},
		{"HcclAlltoAll", "alltoall"},
		{"hcom_alltoall_AicpuKernel_503_1_1", "alltoall"},
		{"HcclAlltoAllv", "alltoallv"},
		{"HcclAlltoAllSingle", "alltoallsingle"},
		{"HcclBroadcast", ""},
		{"hcom_broadcast_123_1_1", ""},
		{"hcom_scatter_123_1_1", "scatter"},
		{"hcom_gather_123_1_1", "gather"},
		// Non-whitelist names → "".
		{"hcom_reduceScatter_AicpuKernel_503_490_1", ""},
		{"hcom_allReduce__503_96_4", ""},
		{"HcclAllreduce", ""},
		{"HcclReduceScatter", ""},
		{"hcom_send_1_2_1", ""},
		{"hcom_recv_1_2_1", ""},
	}
	for _, c := range cases {
		if got := pureCommKind(c.name); got != c.want {
			t.Errorf("pureCommKind(%q) = %q, want %q", c.name, got, c.want)
		}
	}
}

func TestOpSeqB(t *testing.T) {
	if got := opSeqB("hcom_allReduce__503_96_4"); got != 96 {
		t.Errorf("opSeqB = %d, want 96", got)
	}
	if got := opSeqB("hcom_allReduce__0_0_0"); got != 0 {
		t.Errorf("opSeqB = %d, want 0", got)
	}
	if got := opSeqB("HcclAllreduce"); got != -1 {
		t.Errorf("opSeqB = %d, want -1 (no sequence marker)", got)
	}
}

// TestComputeBandwidthFromOps checks alignment, shortest-duration selection,
// and (opType,count) grouping with two ranks whose clocks overlap (wall-clock
// matching). Rank 1 is strictly slower (larger durations), so the shortest
// duration must come from rank 0.
func TestComputeBandwidthFromOps(t *testing.T) {
	members := map[int][]bwOp{
		0: {
			{opType: "allReduce", count: 1000, start: 0, end: 100},
			{opType: "allReduce", count: 1000, start: 1000, end: 1150},
			{opType: "allGather", count: 2000, start: 500, end: 600},
		},
		1: {
			{opType: "allReduce", count: 1000, start: 0, end: 200},
			{opType: "allReduce", count: 1000, start: 1000, end: 1300},
			{opType: "allGather", count: 2000, start: 500, end: 900},
		},
	}
	ranks := []int{0, 1}

	res := computeBandwidthFromOps(members, ranks)
	if len(res) != 2 {
		t.Fatalf("expected 2 combos, got %d", len(res))
	}

	// (allReduce,1000): fastest 10% of durations [100,150] = [100] -> bw = 10.
	ar := res[bucketKey{"allReduce", 1000}]
	if math.Abs(ar-10) > 1e-9 {
		t.Errorf("allReduce bandwidth = %v, want 10", ar)
	}

	// (allGather,2000): fastest 10% of [100] -> bw = 20.
	ag := res[bucketKey{"allGather", 2000}]
	if math.Abs(ag-20) > 1e-9 {
		t.Errorf("allGather bandwidth = %v, want 20", ag)
	}
}

// TestComputeBandwidthFromOpsSeqAlignment verifies the sequence-B fallback:
// when a rank's clock does not overlap the base rank's at all, it is matched by
// op-name sequence index instead.
func TestComputeBandwidthFromOpsSeqAlignment(t *testing.T) {
	// Rank 1's timestamps share no overlap with rank 0 (different time base).
	members := map[int][]bwOp{
		0: {
			{opType: "allReduce", seqB: 7, count: 1000, start: 1000000000, end: 1000000100},
			{opType: "allReduce", seqB: 8, count: 1000, start: 1000001000, end: 1000001200},
		},
		1: {
			{opType: "allReduce", seqB: 7, count: 1000, start: 9000000000000, end: 9000000000200},
			{opType: "allReduce", seqB: 8, count: 1000, start: 9000001000000, end: 9000001000500},
		},
	}
	ranks := []int{0, 1}

	res := computeBandwidthFromOps(members, ranks)
	if len(res) != 1 {
		t.Fatalf("expected 1 combo, got %d", len(res))
	}
	// Two aligned occurrences: fastest 10% of [100,200] = [100] -> bw = 10.
	got := res[bucketKey{"allReduce", 1000}]
	want := 10.0
	if math.Abs(got-want) > 1e-9 {
		t.Errorf("bandwidth = %v, want %v", got, want)
	}
}

// TestBandwidthFor checks the G-elements/s formula.
func TestBandwidthFor(t *testing.T) {
	// 1000 elements / 100 ns = 10 G elements/s.
	if got := bandwidthFor(1000, 100); math.Abs(got-10) > 1e-9 {
		t.Errorf("bandwidthFor(1000,100) = %v, want 10", got)
	}
	if got := bandwidthFor(1000, 0); got != 0 {
		t.Errorf("bandwidthFor with zero duration = %v, want 0", got)
	}
}
