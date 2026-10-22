package center

import (
	"bytes"
	"crypto/rand"
	"encoding/csv"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/config"
	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/profiling/detector"
	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/report"
	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/utils"
)

const (
	probeInterval   = 5 * time.Second
	probeFailLimit  = 3  // consecutive healthz failures → 断连
	reportBufferSec = 60 // fixed buffer added to max collect-wait
)

// heartbeatLoop probes every daemon (healthz + match_status) forever.
func (c *Center) heartbeatLoop() {
	ticker := time.NewTicker(probeInterval)
	defer ticker.Stop()
	for range ticker.C {
		c.mu.Lock()
		for _, b := range c.biz {
			for _, d := range b.Daemons {
				c.probeDaemonLocked(d)
			}
		}
		c.mu.Unlock()
	}
}

// scheduleLoop triggers each business when its interval elapses (paused
// businesses are skipped).
func (c *Center) scheduleLoop() {
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for range ticker.C {
		c.mu.Lock()
		for _, b := range c.biz {
			if b.Paused {
				continue
			}
			interval := c.businessInterval(b)
			if b.nextTrigger.IsZero() {
				b.nextTrigger = time.Now().Add(interval)
			}
			if time.Now().After(b.nextTrigger) {
				// Re-anchor regardless; skip when a round is already in flight.
				b.nextTrigger = time.Now().Add(interval)
				if !b.triggering && c.businessReadyLocked(b) {
					go c.triggerBusiness(b)
				}
			}
		}
		c.mu.Unlock()
	}
}

// businessReadyLocked reports whether every daemon is healthy & matched (a
// frozen business is skipped until the user reconnects/removes the bad daemon).
func (c *Center) businessReadyLocked(b *Business) bool {
	if len(b.Daemons) == 0 {
		return false
	}
	for _, d := range b.Daemons {
		if d.matchState != "healthy" {
			return false
		}
	}
	return true
}

// businessInterval returns a business's trigger period, falling back to the
// center default when unset/non-positive. Both scheduleLoop and triggerBusiness
// must use this so nextTrigger is never anchored in the past.
func (c *Center) businessInterval(b *Business) time.Duration {
	if b.IntervalSec > 0 {
		return time.Duration(b.IntervalSec) * time.Second
	}
	return c.cfg.Interval
}

// ---------------------------------------------------------------------------
// Matching
// ---------------------------------------------------------------------------

func randomKey() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// matchDaemonLocked generates a key and matches a daemon to this center. The
// daemon replies with its collect-wait (used for the report timeout).
func (c *Center) matchDaemonLocked(b *Business, d *Daemon) error {
	d.Key = randomKey()
	// Persist the key immediately so a center restart re-authenticates with the
	// SAME key (not an empty or freshly-random one). Callers hold c.mu.
	c.save()
	body := map[string]any{
		"center_addr": c.selfURL(),
		"key":         d.Key,
		"business":    b.Name,
		"daemon":      d.Addr(),
	}
	resp, err := doJSON(http.MethodPost, d.BaseURL()+"/daemon/match", body, "")
	if err != nil {
		d.Key = ""
		c.save()
		return err
	}
	cw, _ := resp["collect_wait"].(float64)
	d.collectWait = int64(cw)
	d.matchState = "healthy"
	d.healthy = true
	d.failCount = 0
	return nil
}

// unmatchDaemonLocked asks a matched daemon to unmatch (best-effort).
func (c *Center) unmatchDaemonLocked(d *Daemon) {
	if d.Key == "" {
		return
	}
	_, _ = doJSON(http.MethodPost, d.BaseURL()+"/daemon/unmatch", nil, d.Key)
	d.Key = ""
	d.matchState = ""
	d.healthy = false
	d.lastOpMetric = nil
	d.lastReportAt = time.Time{}
}

// ---------------------------------------------------------------------------
// Probing (healthz + match_status → 4-state classification)
// ---------------------------------------------------------------------------

func (c *Center) probeDaemonLocked(d *Daemon) {
	if !daemonHealth(d) {
		d.failCount++
		if d.failCount >= probeFailLimit {
			d.healthy = false
			d.matchState = "" // 断连
		}
		return
	}
	d.failCount = 0
	d.healthy = true

	// healthz OK — distinguish "matched to us" vs "unmatched" vs "other center".
	st, err := daemonMatchStatus(d, d.Key)
	if err != nil {
		d.matchState = ""
		return
	}
	switch {
	case st.Matched:
		d.matchState = "healthy"
		// collect_wait is runtime-only; recover it on every probe so a center
		// restart (which drops it) re-learns it without a full re-match.
		if st.CollectWait > 0 {
			d.collectWait = st.CollectWait
		}
	case st.Other:
		d.matchState = "other"
	default:
		d.matchState = "unmatched"
	}
}

func daemonHealth(d *Daemon) bool {
	resp, err := httpGet(d.BaseURL() + "/healthz")
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 64))
	return resp.StatusCode == http.StatusOK && strings.TrimSpace(string(body)) == "ok"
}

type matchStatusResp struct {
	State       string `json:"state"`
	Matched     bool   `json:"matched"`
	Other       bool   `json:"other"`
	CollectWait int64  `json:"collect_wait"`
}

func daemonMatchStatus(d *Daemon, key string) (*matchStatusResp, error) {
	var st matchStatusResp
	err := getJSON(d.BaseURL()+"/daemon/match_status", key, &st)
	return &st, err
}

// ---------------------------------------------------------------------------
// Trigger + merged detection
// ---------------------------------------------------------------------------

// triggerBusiness triggers every healthy daemon, waits for their reports (or
// the max-collect-wait+60s timeout), merges and re-runs detection. It tracks the
// business's cycle counters and next trigger, and re-anchors the schedule.
// Single-flight: a concurrent trigger for the same business is a no-op.
func (c *Center) triggerBusiness(b *Business) {
	triggerAt := time.Now()

	c.mu.Lock()
	if b.triggering {
		c.mu.Unlock()
		return
	}
	b.triggering = true
	if b.progress == nil {
		b.progress = newProgressLog()
	}
	prog := b.progress
	round := b.CyclesTotal + b.CyclesFailed + 1

	var timeout time.Duration
	healthy := 0
	var addrs []string
	for _, d := range b.Daemons {
		if d.matchState != "healthy" {
			continue
		}
		healthy++
		addrs = append(addrs, d.Addr())
		if cw := d.collectWait; cw > 0 && time.Duration(cw)*time.Second > timeout {
			timeout = time.Duration(cw) * time.Second
		}
		d.lastReportAt = time.Time{} // reset: wait for a fresh report
	}
	c.mu.Unlock()
	timeout += reportBufferSec * time.Second

	prog.begin(round, triggerAt)
	prog.step("业务 %s 检测开始（%d 个已匹配守护进程）", b.Name, healthy)
	prog.step("触发守护进程: %s", strings.Join(addrs, ", "))

	for _, d := range b.Daemons {
		if d.matchState == "healthy" {
			go c.triggerDaemon(d)
		}
	}

	prog.step("等待 op_metric 回传（超时 %s）", timeout)
	deadline := time.Now().Add(timeout)
	reported := -1
	for time.Now().Before(deadline) {
		c.mu.Lock()
		done := true
		n := 0
		for _, d := range b.Daemons {
			if d.matchState != "healthy" {
				continue
			}
			if d.lastReportAt.Before(triggerAt) {
				done = false
			} else {
				n++
			}
		}
		c.mu.Unlock()
		if n != reported {
			reported = n
			prog.step("已回传 op_metric: %d/%d", n, healthy)
		}
		if done {
			break
		}
		time.Sleep(500 * time.Millisecond)
	}

	c.markMissingReports(b, triggerAt)

	merged := c.mergeOpMetric(b)
	durationMs := time.Since(triggerAt).Milliseconds()
	prog.step("合并 op_metric: %d 个 rank", len(merged))

	var failed bool
	if len(merged) == 0 {
		c.logf("business %s: no op_metric reported", b.Name)
		prog.step("本轮无 op_metric 上报，跳过检测")
		failed = true
	} else {
		prog.step("跨节点合并检测中 ...")
		if err := c.detectAndStore(b, merged, triggerAt, durationMs); err != nil {
			c.logf("business %s: detect failed: %v", b.Name, err)
			prog.step("检测失败: %v", err)
			failed = true
		}
	}

	c.mu.Lock()
	if failed {
		b.CyclesFailed++
	} else {
		b.CyclesTotal++
	}
	// Anchor the next trigger to THIS round's start (not its end), so the cadence
	// stays a fixed interval and does not drift by the detection duration.
	b.nextTrigger = triggerAt.Add(c.businessInterval(b))
	b.triggering = false
	c.save()
	c.mu.Unlock()

	if failed {
		prog.step("本轮结束（失败，耗时 %dms）", durationMs)
	} else {
		prog.step("本轮结束（耗时 %dms）", durationMs)
	}
	prog.finish()
	// Persist this round's stage log under its result dir so history can show it
	// (the in-memory log only covers the current/last round).
	_ = prog.save(filepath.Join(c.cfg.DataDir, b.Name, triggerAt.Format("20060102-150405"), "progress.json"))
}

// markMissingReports flags daemons that failed to report within the round's
// deadline (distinct from "断连": healthz is fine but the daemon lost its match
// or hung). The next probe re-classifies them automatically once they recover.
func (c *Center) markMissingReports(b *Business, triggerAt time.Time) {
	c.mu.Lock()
	defer c.mu.Unlock()
	for _, d := range b.Daemons {
		if d.matchState == "healthy" && d.lastReportAt.Before(triggerAt) {
			d.matchState = "report_failed"
		}
	}
}

func (c *Center) triggerDaemon(d *Daemon) {
	_, _ = doJSON(http.MethodPost, d.BaseURL()+"/daemon/trigger", nil, d.Key)
}

func (c *Center) mergeOpMetric(b *Business) detector.OpMetric {
	c.mu.Lock()
	defer c.mu.Unlock()
	merged := make(detector.OpMetric)
	for _, d := range b.Daemons {
		if d.lastOpMetric == nil {
			continue
		}
		for rank, r := range d.lastOpMetric {
			merged[rank] = r
		}
	}
	return merged
}

// detectAndStore runs detection on the merged op_metric and stores the result
// under the business's results dir (same shape as a single daemon's output). The
// op_metric is materialized to a temp dir and re-used through the existing
// file-backed detection path, so host-level (slow-CPU / cross-node) detection
// and the report's cross-node sections work exactly as in a daemon.
func (c *Center) detectAndStore(b *Business, op detector.OpMetric, startedAt time.Time, durationMs int64) error {
	// Detection uses process-wide config globals (FilePath + thresholds), so
	// serialize it across businesses to avoid cross-business races.
	c.detectMu.Lock()
	defer c.detectMu.Unlock()

	tmp, err := restoreOpMetric(op)
	if err != nil {
		return fmt.Errorf("restore op_metric: %w", err)
	}
	defer os.RemoveAll(tmp)

	config.FilePath = tmp
	config.Apply(b.Thresholds)

	parallels, validRanks := detector.GetCurDetectionInfo(tmp)
	if len(validRanks) == 0 {
		return fmt.Errorf("no valid ranks")
	}
	stepData := detector.GetCurJobLastStepData(validRanks)
	result := detector.DelimitDetection(stepData, parallels, validRanks)
	nodeOut, err := utils.BuildNodeResult(result, parallels, nil)
	if err != nil {
		return fmt.Errorf("build node result: %w", err)
	}
	c.mu.Lock()
	addrs := make([]string, 0, len(b.Daemons))
	for _, d := range b.Daemons {
		addrs = append(addrs, d.Addr())
	}
	c.mu.Unlock()
	source := strings.Join(addrs, ",")
	reportText := report.GenerateReport(stepData, parallels, validRanks, result, source)

	summary := map[string]int{
		"cal":        len(result["cal"]),
		"comm":       len(result["comm"]),
		"cpu":        len(result["cpu"]),
		"npu_bubble": len(result["npu_bubble"]),
	}
	if b.progress != nil {
		b.progress.step("检测完成: 慢计算 %d / 慢通信 %d / 慢CPU %d / Bubble %d",
			summary["cal"], summary["comm"], summary["cpu"], summary["npu_bubble"])
	}

	dir := startedAt.Format("20060102-150405")
	if err := c.writeBusinessResult(b, dir, nodeOut, reportText, op, summary, startedAt, durationMs, len(op)); err != nil {
		return fmt.Errorf("store result: %w", err)
	}
	c.logf("business %s: detection done (%d ranks, %d cal anomalies)", b.Name, len(validRanks), len(result["cal"]))
	return nil
}

// restoreOpMetric materializes an in-memory op_metric into a temp dir laid out
// like a daemon's op_metric/ (group_info/host_info/npu_info JSON + global_rank
// CSV) so the file-backed detector can consume it.
func restoreOpMetric(op detector.OpMetric) (string, error) {
	tmp, err := os.MkdirTemp("", "center_opmetric_")
	if err != nil {
		return "", err
	}
	metricDir := filepath.Join(tmp, "op_metric")
	if err := os.MkdirAll(metricDir, 0o755); err != nil {
		return "", err
	}
	for rankStr, r := range op {
		if r == nil {
			continue
		}
		if r.GroupInfo != nil {
			if data, err := json.Marshal(r.GroupInfo); err == nil {
				_ = os.WriteFile(filepath.Join(metricDir, "group_info_"+rankStr+".json"), data, 0o644)
			}
		}
		if r.HostInfo != nil {
			if data, err := json.Marshal(r.HostInfo); err == nil {
				_ = os.WriteFile(filepath.Join(metricDir, "host_info_"+rankStr+".json"), data, 0o644)
			}
		}
		if r.NpuInfo != nil {
			if data, err := json.Marshal(r.NpuInfo); err == nil {
				_ = os.WriteFile(filepath.Join(metricDir, "npu_info_"+rankStr+".json"), data, 0o644)
			}
		}
		if r.GlobalRank != nil {
			_ = writeGlobalRankCSV(filepath.Join(metricDir, "global_rank_"+rankStr+".csv"), r.GlobalRank)
		}
	}
	return tmp, nil
}

// writeGlobalRankCSV writes a global_rank value (single object or array) as a
// CSV with the object keys as header, matching the daemon's global_rank_{N}.csv.
func writeGlobalRankCSV(path string, g any) error {
	var rows []map[string]any
	switch v := g.(type) {
	case map[string]any:
		rows = []map[string]any{v}
	case []any:
		for _, item := range v {
			if m, ok := item.(map[string]any); ok {
				rows = append(rows, m)
			}
		}
	}
	if len(rows) == 0 {
		return nil
	}
	var cols []string
	seen := make(map[string]bool)
	for _, row := range rows {
		for k := range row {
			if !seen[k] {
				seen[k] = true
				cols = append(cols, k)
			}
		}
	}
	var buf bytes.Buffer
	w := csv.NewWriter(&buf)
	if err := w.Write(cols); err != nil {
		return err
	}
	for _, row := range rows {
		rec := make([]string, len(cols))
		for i, col := range cols {
			rec[i] = fmt.Sprintf("%v", row[col])
		}
		if err := w.Write(rec); err != nil {
			return err
		}
	}
	w.Flush()
	return os.WriteFile(path, buf.Bytes(), 0o644)
}

// writeBusinessResult stores a merged detection's output under the business
// results dir, mirroring a single daemon's per-cycle archive shape: result JSON,
// text report, the merged op_metric JSON, plus a cycle.json (ts + trigger time +
// duration + per-category anomaly counts) for the per-business history.
func (c *Center) writeBusinessResult(b *Business, dirRel string, nodeOut *utils.NodeOutput, reportText string, op detector.OpMetric, summary map[string]int, startedAt time.Time, durationMs int64, worldSize int) error {
	base := filepath.Join(c.cfg.DataDir, b.Name, dirRel)
	if err := os.MkdirAll(filepath.Join(base, "analysis_result"), 0o755); err != nil {
		return err
	}
	out, _ := json.MarshalIndent(map[string]any{"profiler": nodeOut}, "", "  ")
	if err := os.WriteFile(filepath.Join(base, "straggler_output.json"), out, 0o644); err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(base, "analysis_result", "detection_report.log"), []byte(reportText), 0o644); err != nil {
		return err
	}
	opData, _ := json.MarshalIndent(op, "", "  ")
	if err := os.WriteFile(filepath.Join(base, "op_metric.json"), opData, 0o644); err != nil {
		return err
	}
	meta, _ := json.MarshalIndent(map[string]any{
		"ts":          dirRel,
		"started_at":  startedAt.Format(time.RFC3339),
		"duration_ms": durationMs,
		"world_size":  worldSize,
		"summary":     summary,
	}, "", "  ")
	return os.WriteFile(filepath.Join(base, "cycle.json"), meta, 0o644)
}

// ---------------------------------------------------------------------------
// HTTP helpers
// ---------------------------------------------------------------------------

func (c *Center) selfURL() string {
	return fmt.Sprintf("http://127.0.0.1:%d", c.cfg.Port)
}

func httpGet(url string) (*http.Response, error) {
	client := &http.Client{Timeout: 3 * time.Second}
	return client.Get(url)
}

func getJSON(url, key string, out any) error {
	req, _ := http.NewRequest(http.MethodGet, url, nil)
	if key != "" {
		req.Header.Set("X-Match-Key", key)
	}
	client := &http.Client{Timeout: 3 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	return json.NewDecoder(resp.Body).Decode(out)
}

func doJSON(method, url string, body any, key string) (map[string]any, error) {
	var rdr io.Reader
	if body != nil {
		data, _ := json.Marshal(body)
		rdr = bytes.NewReader(data)
	}
	req, err := http.NewRequest(method, url, rdr)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	if key != "" {
		req.Header.Set("X-Match-Key", key)
	}
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	if resp.StatusCode >= 300 {
		return out, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	return out, nil
}
