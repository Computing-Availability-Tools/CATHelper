package dataparse

import (
	"database/sql"
	"encoding/csv"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/config"
)

// ---------------------------------------------------------------------------
// Slow-domain bandwidth backfill
//
// This is the "global backfill pass" that runs after every rank has been
// parsed (DataParsing / StartProcess): it re-rescans the .db files, rebuilds
// the parallel topology, aligns collective communication ops across the ranks
// of each domain group, computes a per-(opType,count) bandwidth using the
// shortest duration in the group (the slow rank doesn't wait, so that value is
// closest to the true transfer time), and writes those bandwidths back into the
// existing op_metric/global_rank_{N}.csv files as dynamic columns named
// "<domain>_<opType>_<count>".
//
// The method (align + shortest-duration bandwidth + cross-group comparison)
// is only valid for collective communication domains. Point-to-point domains
// (pp) and Send/Recv ops are skipped.
// ---------------------------------------------------------------------------

const (
	ppDomainName         = "pp"
	embdDomainName       = "embd"
	wallclockToleranceNs = 5e6 // 5 ms: widen the overlap-match window for tiny phase differences (overlap is still required)
)

// seqBRe extracts the sequence index "B" from an op name ("hcom_xxx__A_B_C").
var seqBRe = regexp.MustCompile(`__\d+_(\d+)_\d+$`)

// pureCommTypes is the PURE_COMM_TYPES whitelist: only these pure-collective
// operator families participate in slow-communication bandwidth detection.
var pureCommTypes = map[string]bool{
	"allgather":      true,
	"allgatherv":     true,
	"allgatherbase":  true,
	"alltoall":       true,
	"alltoallv":      true,
	"alltoallsingle": true,
	"scatter":        true,
	"gather":         true,
}

// pureCommKind normalizes an op name to its pure-collective token, or "" when
// it is not in the whitelist (allReduce/reduceScatter/Send/Recv and unknown
// names all yield "" and are skipped). Matching: strip the vendor prefix
// (hcom/Hccl/acl, with optional "_"), take the leading run of letters, then
// lowercase and look it up.
func pureCommKind(nm string) string {
	s := stripVendorPrefix(nm)
	s = leadingLetters(s)
	s = strings.ToLower(s)
	if pureCommTypes[s] {
		return s
	}
	return ""
}

func stripVendorPrefix(nm string) string {
	lower := strings.ToLower(nm)
	for _, p := range []string{"hcom", "hccl", "acl"} {
		if strings.HasPrefix(lower, p) {
			return strings.TrimPrefix(nm[len(p):], "_")
		}
	}
	return nm
}

func leadingLetters(s string) string {
	i := 0
	for i < len(s) {
		c := s[i]
		if (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') {
			i++
		} else {
			break
		}
	}
	return s[:i]
}

// opSeqB extracts the sequence index "B" from an op name
// ("hcom_xxx__A_B_C" -> B), used to align ops when two ranks' clocks are not
// comparable. Returns -1 when the name carries no sequence marker.
func opSeqB(nm string) int {
	m := seqBRe.FindStringSubmatch(nm)
	if m == nil {
		return -1
	}
	n, _ := strconv.Atoi(m[1])
	return n
}

// ---------------------------------------------------------------------------
// Alignment structures
// ---------------------------------------------------------------------------

// bucketKey identifies one (opType × count) bucket used for cross-rank matching.
type bucketKey struct {
	opType string
	count  int
}

// bwOp is one communication operator's parsed data used for bandwidth stats.
type bwOp struct {
	opType string
	seqB   int
	count  int
	start  int
	end    int
}

// bwIndex buckets a rank's collective ops by (opType, count) and sorts each
// bucket by startNs so both wall-clock and sequence matching stay efficient.
type bwIndex struct {
	ops     []bwOp
	buckets map[bucketKey][]int
	starts  map[bucketKey][]int
}

func newBWIndex(ops []bwOp) *bwIndex {
	idx := &bwIndex{
		ops:     ops,
		buckets: make(map[bucketKey][]int),
		starts:  make(map[bucketKey][]int),
	}
	for i, op := range ops {
		k := bucketKey{op.opType, op.count}
		idx.buckets[k] = append(idx.buckets[k], i)
	}
	for k, v := range idx.buckets {
		sort.Slice(v, func(x, y int) bool { return ops[v[x]].start < ops[v[y]].start })
		starts := make([]int, len(v))
		for j, idx := range v {
			starts[j] = ops[idx].start
		}
		idx.starts[k] = starts
	}
	return idx
}

// wallclock matches a reference op against this rank's ops of the same
// (opType, count) by their temporal overlap: the candidate with the greatest
// positive overlap wins. The tolerance widens the candidate window to
// [op.start-tol, op.end), but an op with no overlap is never matched (two ops
// that merely start within tol of each other but do not overlap do not look
// like one collective communication on a Gantt chart). Returns the ops index
// or -1.
func (b *bwIndex) wallclock(op bwOp, tol int) int {
	k := bucketKey{op.opType, op.count}
	bucket := b.buckets[k]
	if len(bucket) == 0 {
		return -1
	}
	starts := b.starts[k]
	n := len(bucket)

	// Greatest overlap among candidates with start in [op.start-tol, op.end).
	lo := sort.SearchInts(starts, op.start-tol)
	hi := sort.SearchInts(starts, op.end)
	best, bov := -1, 0
	for j := lo; j < hi && j < n; j++ {
		idx := bucket[j]
		o := b.ops[idx]
		ov := minInt(op.end, o.end) - maxInt(op.start, o.start)
		if ov > bov {
			bov, best = ov, idx
		}
	}
	if best >= 0 && bov > 0 {
		return best
	}
	return -1
}

// seq matches a reference op by its opName sequence index B (used when a rank's
// clock is not comparable to the group's). Returns the ops index or -1.
func (b *bwIndex) seq(op bwOp) int {
	if op.seqB < 0 {
		return -1
	}
	for _, idx := range b.buckets[bucketKey{op.opType, op.count}] {
		if b.ops[idx].seqB == op.seqB {
			return idx
		}
	}
	return -1
}

// ---------------------------------------------------------------------------
// Bandwidth computation (DB-independent core, unit-testable)
// ---------------------------------------------------------------------------

// computeBandwidthFromOps aligns ops across the group's ranks and returns a
// per-(opType,count) bandwidth (G elements/s) map. Each combo's bandwidth uses
// the mean of the fastest 10% of per-occurrence shortest durations as the
// denominator (the slow rank doesn't wait, so the fastest occurrences are
// closest to the real transfer time).
func computeBandwidthFromOps(members map[int][]bwOp, ranks []int) map[bucketKey]float64 {
	if len(ranks) == 0 {
		return nil
	}
	baseOps := members[ranks[0]]
	if len(baseOps) == 0 {
		return nil
	}

	idxs := make(map[int]*bwIndex, len(ranks))
	for _, r := range ranks {
		idxs[r] = newBWIndex(members[r])
	}

	// Per other rank, decide whether its clock is comparable to the base rank's:
	// if it has any wall-clock match on the base ops it is comparable (use
	// wall-clock); if it has none (e.g. different worker time base) fall back to
	// sequence matching.
	useSeq := make(map[int]bool)
	for _, r := range ranks[1:] {
		wc := 0
		for _, op := range baseOps {
			if idxs[r].wallclock(op, wallclockToleranceNs) >= 0 {
				wc++
			}
		}
		useSeq[r] = wc == 0
	}

	combos := make(map[bucketKey][]int) // (opType,count) -> shortest durations
	for _, base := range baseOps {
		dur := []int{base.end - base.start}
		ok := true
		for _, r := range ranks[1:] {
			var j int
			if useSeq[r] {
				j = idxs[r].seq(base)
			} else {
				j = idxs[r].wallclock(base, wallclockToleranceNs)
			}
			if j < 0 {
				ok = false
				break
			}
			o := members[r][j]
			dur = append(dur, o.end-o.start)
		}
		if !ok {
			continue
		}
		k := bucketKey{base.opType, base.count}
		combos[k] = append(combos[k], minIntSlice(dur))
	}

	res := make(map[bucketKey]float64)
	for k, durs := range combos {
		// Keep positive durations, sort ascending (fastest first), and take the
		// mean of the fastest 10% as the denominator.
		valid := make([]int, 0, len(durs))
		for _, d := range durs {
			if d > 0 {
				valid = append(valid, d)
			}
		}
		if len(valid) == 0 {
			continue
		}
		sort.Ints(valid)
		n := int(math.Ceil(float64(len(valid)) * 0.10))
		if n < 1 {
			n = 1
		}
		var sum int64
		for _, d := range valid[:n] {
			sum += int64(d)
		}
		meanDur := float64(sum) / float64(n)
		res[k] = float64(k.count) / meanDur
	}
	return res
}

// bandwidthFor returns bandwidth in G elements/s for count elements transferred
// in durNs nanoseconds: count / (durNs*1e-9) / 1e9 == count/durNs.
func bandwidthFor(count, durNs int) float64 {
	if durNs <= 0 {
		return 0
	}
	return float64(count) / float64(durNs)
}

// ---------------------------------------------------------------------------
// .db loading
// ---------------------------------------------------------------------------

// rankDB bundles everything needed to load one rank's ops for a domain.
type rankDB struct {
	rankStr string
	path    string
	pgi     map[string]interface{}
}

// loadDomainOps returns the collective (non-Send/Recv) ops belonging to the
// given domain type on one rank's database, filtered to ops with count >=
// minCount.
func loadDomainOps(db *sql.DB, pgi map[string]interface{}, typ string, step StepTime, minCount int) []bwOp {
	var keys []string
	for k, val := range pgi {
		m, ok := val.(map[string]interface{})
		if !ok {
			continue
		}
		if gn, _ := m["group_name"].(string); gn == typ {
			keys = append(keys, k)
		}
	}
	if len(keys) == 0 {
		return nil
	}

	idMap, err := batchQueryStringIDs(db, keys)
	if err != nil || len(idMap) == 0 {
		return nil
	}
	groupNameIDs := make([]int, 0, len(keys))
	for _, k := range keys {
		if id, ok := idMap[k]; ok {
			groupNameIDs = append(groupNameIDs, id)
		}
	}
	if len(groupNameIDs) == 0 {
		return nil
	}

	ops, err := getDeviceOpList(db, groupNameIDs, step)
	if err != nil || len(ops) == 0 {
		return nil
	}

	nameIDs := make([]int, 0, len(ops))
	for _, op := range ops {
		nameIDs = append(nameIDs, op.OpName)
	}
	nameMap := stringMapByIDs(db, nameIDs)

	var out []bwOp
	for _, op := range ops {
		nm := nameMap[op.OpName]
		k := pureCommKind(nm)
		if k == "" {
			continue
		}
		if op.Count < minCount {
			continue
		}
		out = append(out, bwOp{opType: k, seqB: opSeqB(nm), count: op.Count, start: op.StartNs, end: op.EndNs})
	}
	return out
}

// stringMapByIDs resolves STRING_IDS ids to their string values.
func stringMapByIDs(db *sql.DB, ids []int) map[int]string {
	if len(ids) == 0 {
		return nil
	}
	args := make([]interface{}, len(ids))
	for i, id := range ids {
		args[i] = id
	}
	q := fmt.Sprintf("SELECT id, value FROM STRING_IDS WHERE id IN (%s)", placeholders(len(ids)))
	rows, err := db.Query(q, args...)
	if err != nil {
		return nil
	}
	defer rows.Close()

	out := make(map[int]string, len(ids))
	for rows.Next() {
		var id int
		var v string
		if err := rows.Scan(&id, &v); err != nil {
			continue
		}
		out[id] = v
	}
	return out
}

// mergedStep widens a rank's step times to cover the whole profiling window.
func mergedStep(db *sql.DB) StepTime {
	steps, err := GetAllStepTimes(db)
	if err != nil || len(steps) == 0 {
		return StepTime{StartNs: -math.MaxInt, EndNs: math.MaxInt}
	}
	minS, maxE := math.MaxInt, math.MinInt
	for _, s := range steps {
		if s.StartNs < minS {
			minS = s.StartNs
		}
		if s.EndNs > maxE {
			maxE = s.EndNs
		}
	}
	return StepTime{StartNs: minS, EndNs: maxE}
}

// BackfillSlowDomainBandwidth is the global backfill pass entry point (see the
// package comment above). It is safe to call after DataParsing / StartProcess
// and before detection reads the CSVs. Errors are returned but never fatal:
// callers log and continue so detection still runs on the existing Duration
// columns.
func BackfillSlowDomainBandwidth(inputPath string) error {
	dbPaths := discoverDBFiles(inputPath)
	minCount := config.SlowCommMinCount
	if minCount <= 0 {
		minCount = 1000
	}

	// Load per-rank topology.
	rankToInfo := make(map[int]rankDB)
	for _, p := range dbPaths {
		rankStr, err := extractGlobalRankFromFilename(p)
		if err != nil {
			continue
		}
		r, err := strconv.Atoi(rankStr)
		if err != nil {
			continue
		}
		db, err := sql.Open("sqlite", p+"?mode=ro")
		if err != nil {
			continue
		}
		pgi, _, err := readGroupInfo(db, rankStr, inputPath)
		db.Close()
		if err != nil || len(pgi) == 0 {
			continue
		}
		rankToInfo[r] = rankDB{rankStr: rankStr, path: p, pgi: pgi}
	}
	if len(rankToInfo) == 0 {
		return nil
	}

	// Discover domain groups keyed by "type|sorted-ranks".
	type domainGroup struct {
		typ   string
		ranks []int
	}
	groupMap := make(map[string]*domainGroup)
	for _, ri := range rankToInfo {
		for _, val := range ri.pgi {
			m, ok := val.(map[string]interface{})
			if !ok {
				continue
			}
			typ, _ := m["group_name"].(string)
			if typ == "" {
				continue
			}
			var grp []int
			if raw, ok := m["global_ranks"].([]interface{}); ok {
				for _, x := range raw {
					switch n := x.(type) {
					case float64:
						grp = append(grp, int(n))
					case int:
						grp = append(grp, n)
					}
				}
			}
			if len(grp) == 0 {
				continue
			}
			sort.Ints(grp)
			key := typ + "|" + joinRankKey(grp)
			if _, ok := groupMap[key]; !ok {
				groupMap[key] = &domainGroup{typ: typ, ranks: grp}
			}
		}
	}

	// Restrict each group to ranks we actually have db info for.
	for _, g := range groupMap {
		var present []int
		for _, r := range g.ranks {
			if _, ok := rankToInfo[r]; ok {
				present = append(present, r)
			}
		}
		g.ranks = present
	}

	for _, g := range groupMap {
		if g.typ == ppDomainName || g.typ == embdDomainName {
			continue
		}
		if len(g.ranks) < 2 {
			continue
		}

		members := make(map[int][]bwOp, len(g.ranks))
		valid := true
		for _, r := range g.ranks {
			ri := rankToInfo[r]
			db, err := sql.Open("sqlite", ri.path+"?mode=ro")
			if err != nil {
				valid = false
				break
			}
			ops := loadDomainOps(db, ri.pgi, g.typ, mergedStep(db), minCount)
			db.Close()
			members[r] = ops
		}
		if !valid {
			continue
		}

		res := computeBandwidthFromOps(members, g.ranks)
		if len(res) == 0 {
			continue
		}

		cols := make(map[string]string, len(res))
		for k, bw := range res {
			cols[g.typ+"_"+k.opType+"_"+strconv.Itoa(k.count)] = strconv.FormatFloat(bw, 'f', -1, 64)
		}

		for _, r := range g.ranks {
			path := filepath.Join(inputPath, "op_metric", "global_rank_"+strconv.Itoa(r)+".csv")
			if err := backfillBandwidthCSV(path, cols); err != nil {
				fmt.Fprintf(os.Stderr, "[SLOW-DOMAIN] backfill rank %d: %v\n", r, err)
			}
		}
	}
	return nil
}

// ---------------------------------------------------------------------------
// CSV backfill
// ---------------------------------------------------------------------------

// backfillBandwidthCSV appends the given columns (with their per-rank values)
// to an existing global_rank_{N}.csv. Columns already present are skipped.
func backfillBandwidthCSV(path string, cols map[string]string) error {
	csvMutex.Lock()
	defer csvMutex.Unlock()

	f, err := os.Open(path)
	if err != nil {
		return err
	}
	records, rerr := csv.NewReader(f).ReadAll()
	f.Close()
	if rerr != nil {
		return rerr
	}
	if len(records) == 0 {
		return fmt.Errorf("empty csv %s", path)
	}

	header := records[0]
	existing := make(map[string]bool, len(header))
	for _, h := range header {
		existing[h] = true
	}
	var newCols []string
	for name := range cols {
		if !existing[name] {
			newCols = append(newCols, name)
		}
	}
	if len(newCols) == 0 {
		return nil
	}
	sort.Strings(newCols)

	header = append(header, newCols...)
	records[0] = header
	for i := 1; i < len(records); i++ {
		for _, name := range newCols {
			records[i] = append(records[i], cols[name])
		}
	}

	out, err := os.Create(path)
	if err != nil {
		return err
	}
	defer out.Close()
	w := csv.NewWriter(out)
	if err := w.WriteAll(records); err != nil {
		return err
	}
	return nil
}

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

func discoverDBFiles(inputPath string) []string {
	var out []string
	filepath.Walk(inputPath, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return nil
		}
		if info.IsDir() {
			return nil
		}
		base := filepath.Base(path)
		if strings.HasPrefix(base, "ascend_pytorch_profiler_") && strings.HasSuffix(base, ".db") {
			out = append(out, path)
		}
		return nil
	})
	return out
}

func joinRankKey(ranks []int) string {
	parts := make([]string, len(ranks))
	for i, r := range ranks {
		parts[i] = strconv.Itoa(r)
	}
	return strings.Join(parts, ",")
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}

func minIntSlice(vals []int) int {
	if len(vals) == 0 {
		return 0
	}
	mn := vals[0]
	for _, v := range vals[1:] {
		if v < mn {
			mn = v
		}
	}
	return mn
}
