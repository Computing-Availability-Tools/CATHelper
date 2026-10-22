package daemon

import (
	"encoding/csv"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/config"
)

// httpServer builds the net/http mux (standard library only). Routes:
// /status + /straggler/* for queries, /daemon/* for control.
func (d *Daemon) httpServer() *http.Server {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, "ok")
	})
	mux.HandleFunc("GET /{$}", d.handleConsole)
	mux.HandleFunc("GET /status", d.handleStatus)
	mux.HandleFunc("GET /straggler/results/latest", d.handleResultsLatest)
	mux.HandleFunc("GET /straggler/results/history", d.handleResultsHistory)
	mux.HandleFunc("GET /straggler/results/{id}", d.handleResultsByID)
	mux.HandleFunc("GET /straggler/report/latest", d.handleReportLatest)
	mux.HandleFunc("GET /straggler/report/{id}", d.handleReportByID)
	mux.HandleFunc("GET /straggler/op_metric/latest", d.handleOpMetricLatest)
	mux.HandleFunc("GET /straggler/op_metric/{id}", d.handleOpMetricView)
	mux.HandleFunc("GET /straggler/op_metric/{id}/{file}", d.handleOpMetricFile)
	mux.HandleFunc("POST /daemon/start", d.handleDaemonStart)
	mux.HandleFunc("POST /daemon/pause", d.handleDaemonPause)
	mux.HandleFunc("POST /daemon/stop", d.handleDaemonStop)
	mux.HandleFunc("POST /daemon/interval", d.handleDaemonInterval)
	mux.HandleFunc("GET /daemon/thresholds", d.handleDaemonGetThresholds)
	mux.HandleFunc("POST /daemon/thresholds", d.handleDaemonSetThresholds)
	mux.HandleFunc("POST /daemon/trigger", d.handleDaemonTrigger)
	mux.HandleFunc("POST /daemon/match", d.handleDaemonMatch)
	mux.HandleFunc("POST /daemon/unmatch", d.handleDaemonUnmatch)
	mux.HandleFunc("GET /daemon/match_status", d.handleDaemonMatchStatus)
	mux.HandleFunc("GET /straggler/progress/{id}", d.handleProgressByID)
	return &http.Server{Addr: fmt.Sprintf(":%d", d.cfg.Port), Handler: mux}
}

// handleProgressByID returns one cycle's stage log: the in-memory live log for
// the current/last cycle, or the persisted progress.json from that cycle's
// archive dir (so history stays viewable after a restart). id "latest" =
// current/last.
func (d *Daemon) handleProgressByID(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if id == "latest" {
		writeJSON(w, d.progress.snapshot())
		return
	}
	n, err := strconv.Atoi(id)
	if err != nil {
		http.Error(w, "invalid id", http.StatusBadRequest)
		return
	}
	if snap := d.progress.snapshot(); snap.Cycle == n {
		writeJSON(w, snap)
		return
	}
	if c := d.st.get(n); c != nil && c.DumpDir != "" {
		if raw, rerr := os.ReadFile(filepath.Join(c.DumpDir, "progress.json")); rerr == nil {
			var snap progressSnapshot
			if json.Unmarshal(raw, &snap) == nil {
				writeJSON(w, snap)
				return
			}
		}
	}
	writeJSON(w, progressSnapshot{})
}

// handleStatus reports the daemon state, the two data dirs, and session stats.
func (d *Daemon) handleStatus(w http.ResponseWriter, r *http.Request) {
	d.mu.Lock()
	state := d.state
	interval := d.interval
	nextRun := d.nextRun
	managed := state == "managed"
	centerAddr := d.centerAddr
	d.mu.Unlock()
	total, failed := d.st.counts()

	resp := statusResponse{
		State:             state,
		IntervalSec:       int64(interval.Seconds()),
		CollectWait:       int64(d.cfg.CollectWait.Seconds()),
		Thresholds:        d.Thresholds(),
		DefaultThresholds: config.DefaultThresholds(),
		Managed:           managed,
		CenterAddr:        centerAddr,
		ProfilerDir:       d.cfg.ProfilerDir,
		KpiDir:            d.cfg.KpiDir,
		CyclesTotal:       total,
		CyclesFailed:      failed,
	}
	if c := d.st.latest(); c != nil {
		resp.LastCycle = toCycleSummary(c)
	}
	if state == "running" && !nextRun.IsZero() {
		t := nextRun
		resp.NextRunAt = &t
	}
	writeJSON(w, resp)
}

// handleResultsLatest serves the most recent cycle's combined result JSON,
// from this session only (no disk history).
func (d *Daemon) handleResultsLatest(w http.ResponseWriter, r *http.Request) {
	if c := d.st.latest(); c != nil && c.JSONPath != "" && fileExists(c.JSONPath) {
		http.ServeFile(w, r, c.JSONPath)
		return
	}
	http.Error(w, "no result yet", http.StatusNotFound)
}

// handleResultsHistory lists this session's cycle summaries, newest first.
// The full session history is returned by default; ?limit=N caps the list.
func (d *Daemon) handleResultsHistory(w http.ResponseWriter, r *http.Request) {
	limit := 0 // 0 = no cap: all of this session's cycles
	if v := r.URL.Query().Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			limit = n
		}
	}
	cycles := d.st.list()
	if limit > 0 && len(cycles) > limit {
		cycles = cycles[:limit]
	}
	resp := historyResponse{Cycles: make([]*cycleSummary, 0, len(cycles)+1)}
	// Prepend the in-flight cycle so it appears in history (and its progress is
	// reachable) the moment it starts.
	if snap := d.progress.snapshot(); snap.InFlight {
		started, _ := time.Parse(time.RFC3339, snap.Started)
		resp.Cycles = append(resp.Cycles, &cycleSummary{ID: snap.Cycle, StartedAt: started, Running: true})
	}
	for _, c := range cycles {
		resp.Cycles = append(resp.Cycles, toCycleSummary(c))
	}
	writeJSON(w, resp)
}

// handleResultsByID serves one cycle's combined result JSON by id, from this
// session only.
func (d *Daemon) handleResultsByID(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.Atoi(r.PathValue("id"))
	if err != nil {
		http.Error(w, "invalid id", http.StatusBadRequest)
		return
	}
	if c := d.st.get(id); c != nil && c.JSONPath != "" && fileExists(c.JSONPath) {
		http.ServeFile(w, r, c.JSONPath)
		return
	}
	http.NotFound(w, r)
}

// handleReportLatest serves the most recent cycle's text report (text/plain),
// from this session only.
func (d *Daemon) handleReportLatest(w http.ResponseWriter, r *http.Request) {
	if c := d.st.latest(); c != nil && c.Report != "" {
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		_, _ = io.WriteString(w, c.Report)
		return
	}
	http.Error(w, "no report yet", http.StatusNotFound)
}

// handleReportByID serves one cycle's text report (text/plain) by id, from
// this session only (the same in-memory store as /straggler/results/{id}).
func (d *Daemon) handleReportByID(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.Atoi(r.PathValue("id"))
	if err != nil {
		http.Error(w, "invalid id", http.StatusBadRequest)
		return
	}
	c := d.st.get(id)
	if c == nil {
		http.Error(w, fmt.Sprintf("cycle %d not found", id), http.StatusNotFound)
		return
	}
	if c.Report == "" {
		http.Error(w, fmt.Sprintf("no report for cycle %d (cycle failed before detection)", id), http.StatusNotFound)
		return
	}
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	_, _ = io.WriteString(w, c.Report)
}

// handleOpMetricLatest serves the most recent cycle's aggregated op_metric view
// as one JSON document, keyed by rank.
func (d *Daemon) handleOpMetricLatest(w http.ResponseWriter, r *http.Request) {
	c := d.st.latest()
	if c == nil {
		http.Error(w, "no cycle yet", http.StatusNotFound)
		return
	}
	resp, err := buildOpMetricView(c)
	if err != nil {
		http.Error(w, fmt.Sprintf("no op_metric for latest cycle: %v", err), http.StatusNotFound)
		return
	}
	writeJSON(w, resp)
}

// handleOpMetricView serves one cycle's archived op_metric/ as an aggregated
// JSON document. Top-level keys are ranks; each rank maps to its parsed
// {group_info, host_info, global_rank} where global_rank is the CSV turned into
// a JSON object (single data row) or array (multiple rows).
func (d *Daemon) handleOpMetricView(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.Atoi(r.PathValue("id"))
	if err != nil {
		http.Error(w, "invalid id", http.StatusBadRequest)
		return
	}
	c := d.st.get(id)
	if c == nil {
		http.Error(w, fmt.Sprintf("cycle %d not found", id), http.StatusNotFound)
		return
	}
	resp, err := buildOpMetricView(c)
	if err != nil {
		http.Error(w, fmt.Sprintf("no op_metric for cycle %d (dir unreadable: %v)", id, err), http.StatusNotFound)
		return
	}
	writeJSON(w, resp)
}

// buildOpMetricView reads a cycle's archived op_metric/ dir and aggregates the
// per-rank file kinds into one rank-keyed structure:
//
//	{
//	  "cycle": 3, "dir": "daemon_results/<start>",
//	  "ranks": {
//	    "0": {
//	      "group_info":  {...parallel_group_info...},
//	      "host_info":   {"rank":"0","hostUid":"..."},
//	      "npu_info":    {"rank":"0","id":0},
//	      "global_rank": {"StepIndex":0,"ZP_Kernel":...,"tp_Duration":...}
//	    }, ...
//	  }
//	}
func buildOpMetricView(c *CycleResult) (*opMetricViewResponse, error) {
	dir := filepath.Join(c.DumpDir, "op_metric")
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	resp := &opMetricViewResponse{Cycle: c.ID, Dir: dir, Ranks: make(map[string]opMetricRank)}
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		name := e.Name()
		var rank, kind string
		switch {
		case strings.HasPrefix(name, "group_info_"):
			rank, kind = trimExt(strings.TrimPrefix(name, "group_info_"), ".json"), "group_info"
		case strings.HasPrefix(name, "host_info_"):
			rank, kind = trimExt(strings.TrimPrefix(name, "host_info_"), ".json"), "host_info"
		case strings.HasPrefix(name, "npu_info_"):
			rank, kind = trimExt(strings.TrimPrefix(name, "npu_info_"), ".json"), "npu_info"
		case strings.HasPrefix(name, "global_rank_"):
			rank, kind = trimExt(strings.TrimPrefix(name, "global_rank_"), ".csv"), "global_rank"
		default:
			continue
		}
		if rank == "" {
			continue
		}
		full := filepath.Join(dir, name)
		rv := resp.Ranks[rank]
		switch kind {
		case "group_info":
			rv.GroupInfo, _ = readJSONFile(full)
		case "host_info":
			rv.HostInfo, _ = readJSONFile(full)
		case "npu_info":
			rv.NpuInfo, _ = readJSONFile(full)
		case "global_rank":
			rv.GlobalRank, _ = readCSVFile(full)
		}
		resp.Ranks[rank] = rv
	}
	return resp, nil
}

// trimExt removes a trailing extension from name, returning name unchanged
// when it does not end with ext.
func trimExt(name, ext string) string {
	if strings.HasSuffix(name, ext) {
		return name[:len(name)-len(ext)]
	}
	return name
}

// readJSONFile parses a JSON file into a generic map.
func readJSONFile(path string) (map[string]any, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		return nil, err
	}
	return m, nil
}

// readCSVFile parses a CSV into a JSON object (single data row) or an array of
// objects (multiple rows). Numeric cells become float64, others stay strings.
func readCSVFile(path string) (any, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	rows, err := csv.NewReader(f).ReadAll()
	if err != nil || len(rows) < 2 {
		return nil, fmt.Errorf("csv %s: %v", path, err)
	}
	header := rows[0]
	objs := make([]map[string]any, 0, len(rows)-1)
	for _, row := range rows[1:] {
		obj := make(map[string]any, len(header))
		for i, col := range row {
			if i >= len(header) {
				break
			}
			if v, perr := strconv.ParseFloat(strings.TrimSpace(col), 64); perr == nil {
				obj[header[i]] = v
			} else {
				obj[header[i]] = col
			}
		}
		objs = append(objs, obj)
	}
	if len(objs) == 1 {
		return objs[0], nil
	}
	return objs, nil
}

// handleOpMetricFile serves one file from a cycle's archived op_metric/ dir
// (e.g. group_info_0.json, global_rank_0.csv). Path traversal is blocked by
// rejecting any {file} containing a separator.
func (d *Daemon) handleOpMetricFile(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.Atoi(r.PathValue("id"))
	if err != nil {
		http.Error(w, "invalid id", http.StatusBadRequest)
		return
	}
	name := r.PathValue("file")
	if name == "" || strings.ContainsAny(name, `/\`) {
		http.Error(w, "invalid file name", http.StatusBadRequest)
		return
	}
	c := d.st.get(id)
	if c == nil {
		http.Error(w, fmt.Sprintf("cycle %d not found", id), http.StatusNotFound)
		return
	}
	full := filepath.Join(c.DumpDir, "op_metric", name)
	if !fileExists(full) {
		http.Error(w, fmt.Sprintf("%s not found for cycle %d", name, id), http.StatusNotFound)
		return
	}
	http.ServeFile(w, r, full)
}

func (d *Daemon) handleDaemonStart(w http.ResponseWriter, r *http.Request) {
	if d.IsManaged() {
		http.Error(w, "managed: start is not supported; unmatch first", http.StatusForbidden)
		return
	}
	d.Start()
	writeJSON(w, map[string]string{"state": "running"})
}

func (d *Daemon) handleDaemonPause(w http.ResponseWriter, r *http.Request) {
	if d.IsManaged() {
		http.Error(w, "managed: pause is not supported; unmatch first", http.StatusForbidden)
		return
	}
	d.Pause()
	writeJSON(w, map[string]string{"state": "paused"})
}

func (d *Daemon) handleDaemonInterval(w http.ResponseWriter, r *http.Request) {
	if d.IsManaged() {
		http.Error(w, "managed: interval is not supported; unmatch first", http.StatusForbidden)
		return
	}
	var req struct {
		IntervalSec int64 `json:"interval_sec"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid body: {\"interval_sec\": 300}", http.StatusBadRequest)
		return
	}
	if err := d.SetInterval(req.IntervalSec); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	writeJSON(w, struct {
		IntervalSec int64 `json:"interval_sec"`
	}{req.IntervalSec})
}

// handleDaemonGetThresholds returns the current detection thresholds.
func (d *Daemon) handleDaemonGetThresholds(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, d.Thresholds())
}

// handleDaemonSetThresholds updates the detection thresholds for subsequent
// cycles.
func (d *Daemon) handleDaemonSetThresholds(w http.ResponseWriter, r *http.Request) {
	if !d.requireManagedKey(w, r) {
		return
	}
	var req config.Thresholds
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "无效请求体: {\"cal\",\"cpu\",\"bubble_ns\",\"comm_threshold\",\"comm_min_count\",\"comm_count_floor\"}", http.StatusBadRequest)
		return
	}
	d.SetThresholds(req)
	writeJSON(w, d.Thresholds())
}

func (d *Daemon) handleDaemonTrigger(w http.ResponseWriter, r *http.Request) {
	if !d.requireManagedKey(w, r) {
		return
	}
	if err := d.Trigger(); err != nil {
		http.Error(w, err.Error(), http.StatusConflict)
		return
	}
	writeJSON(w, map[string]string{"status": "triggered"})
}

// handleDaemonStop requests a graceful daemon shutdown: Run's select sees the
// closed stopCh and stops the HTTP server, waits for an in-flight cycle. The
// dynolog child is left running. The response is flushed before the server
// shuts down.
func (d *Daemon) handleDaemonStop(w http.ResponseWriter, r *http.Request) {
	d.Stop()
	writeJSON(w, map[string]string{"status": "stopping"})
}

// handleDaemonMatch establishes a center-node managed relationship. The center
// generates the per-daemon key and passes it here along with the identity under
// which this daemon reports op_metric. The response echoes collect_wait so the
// center can compute the report timeout.
func (d *Daemon) handleDaemonMatch(w http.ResponseWriter, r *http.Request) {
	var req struct {
		CenterAddr string `json:"center_addr"`
		Key        string `json:"key"`
		Business   string `json:"business"`
		Daemon     string `json:"daemon"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.CenterAddr == "" || req.Key == "" {
		http.Error(w, `invalid body: {"center_addr","key","business","daemon"}`, http.StatusBadRequest)
		return
	}
	// The center may self-report a loopback/unspecified host; override the host
	// with the request's source IP so the reverse heartbeat reaches it.
	if err := d.Match(resolveCenterAddr(req.CenterAddr, r), req.Key, req.Business, req.Daemon); err != nil {
		http.Error(w, err.Error(), http.StatusConflict)
		return
	}
	writeJSON(w, map[string]any{"status": "matched", "collect_wait": int64(d.cfg.CollectWait.Seconds())})
}

// resolveCenterAddr replaces centerAddr's host with the HTTP request's source IP,
// keeping the port. Falls back to the original when the source addr or URL can't
// be parsed.
func resolveCenterAddr(centerAddr string, r *http.Request) string {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil || host == "" {
		return centerAddr
	}
	u, err := url.Parse(centerAddr)
	if err != nil || u.Port() == "" {
		return centerAddr
	}
	u.Host = net.JoinHostPort(host, u.Port())
	return u.String()
}

// handleDaemonUnmatch ends the managed relationship; requires the match key.
func (d *Daemon) handleDaemonUnmatch(w http.ResponseWriter, r *http.Request) {
	if !d.requireManagedKey(w, r) {
		return
	}
	d.Unmatch()
	writeJSON(w, map[string]string{"status": "unmatched"})
}

// handleDaemonMatchStatus reports the matching state. With the correct key it
// also confirms the caller is the matched center; a wrong key on a managed
// daemon signals "matched to another center".
func (d *Daemon) handleDaemonMatchStatus(w http.ResponseWriter, r *http.Request) {
	state, center, _ := d.matchState()
	if state != "managed" {
		writeJSON(w, map[string]any{"state": state, "matched": false})
		return
	}
	if d.KeyMatches(r.Header.Get("X-Match-Key")) {
		writeJSON(w, map[string]any{
			"state":        state,
			"matched":      true,
			"center":       center,
			"collect_wait": int64(d.cfg.CollectWait.Seconds()),
		})
		return
	}
	writeJSON(w, map[string]any{"state": state, "matched": false, "other": true})
}

// requireManagedKey gates a control endpoint: when NOT managed it passes
// through; when managed it requires the correct X-Match-Key, otherwise 403.
func (d *Daemon) requireManagedKey(w http.ResponseWriter, r *http.Request) bool {
	if !d.IsManaged() {
		return true
	}
	if !d.KeyMatches(r.Header.Get("X-Match-Key")) {
		http.Error(w, "managed: missing or wrong X-Match-Key", http.StatusForbidden)
		return false
	}
	return true
}

func toCycleSummary(c *CycleResult) *cycleSummary {
	if c == nil {
		return nil
	}
	return &cycleSummary{
		ID:         c.ID,
		StartedAt:  c.StartedAt,
		FinishedAt: c.FinishedAt,
		DurationMs: c.DurationMs,
		DBs:        c.DBs,
		DumpDir:    c.DumpDir,
		Summary:    c.Summary,
		KPIStatus:  c.KPIStatus,
		Error:      c.Error,
	}
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_ = json.NewEncoder(w).Encode(v)
}

func fileExists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && !info.IsDir()
}

func fileExistsDir(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.IsDir()
}
