package center

import (
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/config"
	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/profiling/detector"
)

// httpServer builds the center's HTTP mux.
func (c *Center) httpServer() *http.Server {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /{$}", c.handleConsole)
	mux.HandleFunc("GET /center/healthz", c.handleHealthz)
	mux.HandleFunc("GET /center/businesses", c.handleListBusinesses)
	mux.HandleFunc("POST /center/business", c.handleAddBusiness)
	mux.HandleFunc("DELETE /center/business/{name}", c.handleRemoveBusiness)
	mux.HandleFunc("POST /center/business/{name}/daemon", c.handleAddDaemon)
	mux.HandleFunc("DELETE /center/business/{name}/daemon", c.handleRemoveDaemon)
	mux.HandleFunc("POST /center/business/{name}/daemon/match", c.handleMatchDaemon)
	mux.HandleFunc("POST /center/business/{name}/daemon/unmatch", c.handleUnmatchDaemon)
	mux.HandleFunc("POST /center/op_metric/{business}/{daemon}", c.handleOpMetric)
	mux.HandleFunc("GET /center/business/{name}/history", c.handleBusinessHistory)
	mux.HandleFunc("GET /center/business/{name}/progress", c.handleBusinessProgress)
	mux.HandleFunc("GET /center/business/{name}/console", c.handleBusinessConsole)
	mux.HandleFunc("GET /center/chart.umd.min.js", c.handleChartJS)
	mux.HandleFunc("GET /center/business/{name}/report", c.handleBusinessReport)
	mux.HandleFunc("GET /center/business/{name}/result", c.handleBusinessResult)
	mux.HandleFunc("GET /center/business/{name}/op_metric", c.handleBusinessOpMetric)
	mux.HandleFunc("POST /center/business/{name}/trigger", c.handleBusinessTrigger)
	mux.HandleFunc("POST /center/business/{name}/pause", c.handleBusinessPause)
	mux.HandleFunc("POST /center/business/{name}/start", c.handleBusinessStart)
	mux.HandleFunc("POST /center/business/{name}/interval", c.handleBusinessInterval)
	mux.HandleFunc("POST /center/business/{name}/thresholds", c.handleBusinessThresholds)
	mux.HandleFunc("POST /center/business/{name}/vllm", c.handleSetVLLMMetrics)
	mux.HandleFunc("DELETE /center/business/{name}/vllm", c.handleUnsetVLLMMetrics)
	mux.HandleFunc("GET /center/business/{name}/metrics", c.handleBusinessMetrics)
	return &http.Server{Handler: mux}
}

func (c *Center) handleHealthz(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte("ok"))
}

func (c *Center) handleListBusinesses(w http.ResponseWriter, r *http.Request) {
	c.mu.Lock()
	defer c.mu.Unlock()
	writeJSON(w, map[string]any{"businesses": c.statusViewLocked()})
}

// daemonStatus is the web-visible view of one daemon (runtime match/health
// state included).
type daemonStatus struct {
	IP          string `json:"ip"`
	Port        int    `json:"port"`
	State       string `json:"state"` // healthy / unmatched / other / disconnected
	CollectWait int64  `json:"collect_wait"`
}

// businessStatus is the web-visible view of one business.
type businessStatus struct {
	Name              string            `json:"name"`
	IntervalSec       int64             `json:"interval_sec"`
	Paused            bool              `json:"paused"`
	CyclesTotal       int               `json:"cycles_total"`
	CyclesFailed      int               `json:"cycles_failed"`
	NextTrigger       string            `json:"next_trigger,omitempty"`
	CollectWait       int64             `json:"collect_wait"`           // max across the business's daemons
	VLLMMetrics       string            `json:"vllm_metrics,omitempty"` // vllm /metrics endpoint URL
	Thresholds        config.Thresholds `json:"thresholds"`             // merged-detection thresholds
	DefaultThresholds config.Thresholds `json:"default_thresholds"`     // factory defaults, for "restore defaults"
	Daemons           []daemonStatus    `json:"daemons"`
}

func daemonState(d *Daemon) string {
	switch d.matchState {
	case "healthy":
		return "healthy"
	case "unmatched":
		return "unmatched"
	case "other":
		return "other"
	case "report_failed":
		return "report_failed"
	default:
		return "disconnected"
	}
}

func (c *Center) statusViewLocked() []businessStatus {
	out := make([]businessStatus, 0, len(c.biz))
	for _, b := range c.biz {
		out = append(out, businessStatusOfLocked(b))
	}
	// Stable ordering: c.biz is a map whose iteration order is random, so sort
	// by name to keep the console from reshuffling businesses on every poll.
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// businessStatusOfLocked builds the web-safe status view for one business
// (never exposing the daemons' match keys).
func businessStatusOfLocked(b *Business) businessStatus {
	bs := businessStatus{
		Name:              b.Name,
		IntervalSec:       b.IntervalSec,
		Paused:            b.Paused,
		CyclesTotal:       b.CyclesTotal,
		CyclesFailed:      b.CyclesFailed,
		VLLMMetrics:       b.VLLMMetrics,
		Thresholds:        b.Thresholds,
		DefaultThresholds: config.DefaultThresholds(),
		Daemons:           make([]daemonStatus, 0, len(b.Daemons)),
	}
	if !b.nextTrigger.IsZero() {
		bs.NextTrigger = b.nextTrigger.Format(time.RFC3339)
	}
	for _, d := range b.Daemons {
		bs.Daemons = append(bs.Daemons, daemonStatus{IP: d.IP, Port: d.Port, State: daemonState(d), CollectWait: d.collectWait})
		if d.collectWait > bs.CollectWait {
			bs.CollectWait = d.collectWait
		}
	}
	return bs
}

func (c *Center) handleAddBusiness(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Name        string    `json:"name"`
		IntervalSec int64     `json:"interval_sec"`
		Daemons     []*Daemon `json:"daemons"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.Name == "" {
		http.Error(w, `invalid body: {"name","interval_sec","daemons":[{"ip","port"}]}`, http.StatusBadRequest)
		return
	}
	if req.IntervalSec <= 0 {
		req.IntervalSec = int64(c.cfg.Interval.Seconds())
	}
	c.mu.Lock()
	if _, exists := c.biz[req.Name]; exists {
		c.mu.Unlock()
		http.Error(w, "business already exists", http.StatusConflict)
		return
	}
	b := &Business{Name: req.Name, IntervalSec: req.IntervalSec, Daemons: req.Daemons}
	if b.Daemons == nil {
		b.Daemons = []*Daemon{}
	}
	if b.Thresholds == (config.Thresholds{}) {
		b.Thresholds = c.cfg.Thresholds
	} else {
		b.Thresholds = b.Thresholds.Normalized()
	}
	b.progress = newProgressLog()
	c.biz[req.Name] = b
	c.save()
	view := businessStatusOfLocked(b)
	c.mu.Unlock()

	// Attempt to match the newly added daemons (async; the probe loop will
	// classify any that fail as unmatched/other).
	for _, d := range b.Daemons {
		go c.tryMatch(b, d)
	}
	writeJSON(w, view)
}

func (c *Center) handleRemoveBusiness(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	c.mu.Lock()
	b, ok := c.biz[name]
	if !ok {
		c.mu.Unlock()
		http.Error(w, "business not found", http.StatusNotFound)
		return
	}
	for _, d := range b.Daemons {
		c.unmatchDaemonLocked(d)
	}
	delete(c.biz, name)
	c.save()
	c.metrics.clear(name)
	c.mu.Unlock()
	writeJSON(w, map[string]string{"status": "removed"})
}

func (c *Center) handleAddDaemon(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	var req struct {
		IP   string `json:"ip"`
		Port int    `json:"port"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.IP == "" || req.Port <= 0 {
		http.Error(w, `invalid body: {"ip","port"}`, http.StatusBadRequest)
		return
	}
	c.mu.Lock()
	b, ok := c.biz[name]
	if !ok {
		c.mu.Unlock()
		http.Error(w, "business not found", http.StatusNotFound)
		return
	}
	if c.daemonIndex(b, req.IP, req.Port) >= 0 {
		c.mu.Unlock()
		http.Error(w, "daemon already in business", http.StatusConflict)
		return
	}
	d := &Daemon{IP: req.IP, Port: req.Port}
	b.Daemons = append(b.Daemons, d)
	c.save()
	view := businessStatusOfLocked(b)
	c.mu.Unlock()

	go c.tryMatch(b, d)
	writeJSON(w, view)
}

func (c *Center) handleRemoveDaemon(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	ip := r.URL.Query().Get("ip")
	port, _ := strconv.Atoi(r.URL.Query().Get("port"))
	c.mu.Lock()
	b, ok := c.biz[name]
	if !ok {
		c.mu.Unlock()
		http.Error(w, "business not found", http.StatusNotFound)
		return
	}
	idx := c.daemonIndex(b, ip, port)
	if idx < 0 {
		c.mu.Unlock()
		http.Error(w, "daemon not in business", http.StatusNotFound)
		return
	}
	c.unmatchDaemonLocked(b.Daemons[idx])
	b.Daemons = append(b.Daemons[:idx], b.Daemons[idx+1:]...)
	c.save()
	c.mu.Unlock()
	writeJSON(w, map[string]string{"status": "removed"})
}

// handleMatchDaemon re-matches a daemon (user's "reconnect" action for an
// unmatched / other-center daemon).
func (c *Center) handleMatchDaemon(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	var req struct {
		IP   string `json:"ip"`
		Port int    `json:"port"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.IP == "" || req.Port <= 0 {
		http.Error(w, `invalid body: {"ip","port"}`, http.StatusBadRequest)
		return
	}
	c.mu.Lock()
	b, ok := c.biz[name]
	if !ok {
		c.mu.Unlock()
		http.Error(w, "business not found", http.StatusNotFound)
		return
	}
	idx := c.daemonIndex(b, req.IP, req.Port)
	if idx < 0 {
		c.mu.Unlock()
		http.Error(w, "daemon not in business", http.StatusNotFound)
		return
	}
	err := c.matchDaemonLocked(b, b.Daemons[idx])
	c.mu.Unlock()
	if err != nil {
		http.Error(w, "match failed: "+err.Error(), http.StatusBadGateway)
		return
	}
	writeJSON(w, map[string]string{"status": "matched"})
}

// handleUnmatchDaemon manually unmatches a daemon (back to the daemon's own
// "unmatched" state).
func (c *Center) handleUnmatchDaemon(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	var req struct {
		IP   string `json:"ip"`
		Port int    `json:"port"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.IP == "" || req.Port <= 0 {
		http.Error(w, `invalid body: {"ip","port"}`, http.StatusBadRequest)
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	b, ok := c.biz[name]
	if !ok {
		http.Error(w, "business not found", http.StatusNotFound)
		return
	}
	idx := c.daemonIndex(b, req.IP, req.Port)
	if idx < 0 {
		http.Error(w, "daemon not in business", http.StatusNotFound)
		return
	}
	c.unmatchDaemonLocked(b.Daemons[idx])
	writeJSON(w, map[string]string{"status": "unmatched"})
}

// handleOpMetric receives a daemon's reported op_metric JSON (key-authenticated)
// and stores it for the current trigger round.
func (c *Center) handleOpMetric(w http.ResponseWriter, r *http.Request) {
	business := r.PathValue("business")
	daemon := r.PathValue("daemon")
	key := r.Header.Get("X-Match-Key")
	c.logf("op_metric: business=%q daemon=%q keyLen=%d", business, daemon, len(key))

	c.mu.Lock()
	defer c.mu.Unlock()
	b, ok := c.biz[business]
	if !ok {
		http.Error(w, "business not found", http.StatusNotFound)
		return
	}
	for _, d := range b.Daemons {
		if d.Addr() != daemon || d.Key == "" || d.Key != key {
			continue
		}
		body, _ := io.ReadAll(r.Body)
		var op detector.OpMetric
		if err := json.Unmarshal(body, &op); err != nil {
			// Backward-compatible: accept the {cycle,dir,ranks} envelope too.
			var env struct {
				Ranks detector.OpMetric `json:"ranks"`
			}
			if err2 := json.Unmarshal(body, &env); err2 != nil || env.Ranks == nil {
				c.logf("op_metric decode error: %v (fallback: %v)", err, err2)
				http.Error(w, "invalid op_metric JSON: "+err.Error(), http.StatusBadRequest)
				return
			}
			op = env.Ranks
		}
		d.lastOpMetric = op
		d.lastReportAt = time.Now()
		writeJSON(w, map[string]string{"status": "ok"})
		return
	}
	c.logf("op_metric not matched: daemon %q (biz daemons=%d)", daemon, len(b.Daemons))
	http.Error(w, "daemon not matched to this center", http.StatusForbidden)
}

// handleBusinessHistory lists the business's per-cycle records (ts + summary),
// newest first.
func (c *Center) handleBusinessHistory(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, map[string]any{"cycles": c.cycleInfos(r.PathValue("name"))})
}

// handleBusinessProgress returns a round's stage log: ?ts= selects a specific
// round (from its persisted progress.json, or the live in-memory log when it is
// the current one); omitted = the current/last round.
func (c *Center) handleBusinessProgress(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	ts := r.URL.Query().Get("ts")
	c.mu.Lock()
	b := c.biz[name]
	var p *progressLog
	if b != nil {
		p = b.progress
	}
	c.mu.Unlock()
	if b == nil {
		http.Error(w, "business not found", http.StatusNotFound)
		return
	}

	if ts == "" {
		if p != nil {
			writeJSON(w, p.snapshot())
			return
		}
		writeJSON(w, progressSnapshot{})
		return
	}

	if p != nil {
		if snap := p.snapshot(); snap.InFlight && snap.StartTs == ts {
			writeJSON(w, snap)
			return
		}
	}
	if raw, err := os.ReadFile(filepath.Join(c.cfg.DataDir, name, ts, "progress.json")); err == nil {
		var snap progressSnapshot
		if json.Unmarshal(raw, &snap) == nil {
			writeJSON(w, snap)
			return
		}
	}
	writeJSON(w, progressSnapshot{})
}

// handleBusinessReport serves the business's merged detection report (text/plain)
// for a given cycle (?ts=), latest when omitted.
func (c *Center) handleBusinessReport(w http.ResponseWriter, r *http.Request) {
	dir := c.resultDir(r.PathValue("name"), r.URL.Query().Get("ts"))
	if dir == "" {
		http.Error(w, "no result yet", http.StatusNotFound)
		return
	}
	path := filepath.Join(dir, "analysis_result", "detection_report.log")
	raw, err := os.ReadFile(path)
	if err != nil {
		http.Error(w, "no report yet", http.StatusNotFound)
		return
	}
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	_, _ = w.Write(raw)
}

// handleBusinessResult serves the business's merged result JSON for a given
// cycle (?ts=), latest when omitted.
func (c *Center) handleBusinessResult(w http.ResponseWriter, r *http.Request) {
	dir := c.resultDir(r.PathValue("name"), r.URL.Query().Get("ts"))
	if dir == "" {
		http.Error(w, "no result yet", http.StatusNotFound)
		return
	}
	http.ServeFile(w, r, filepath.Join(dir, "straggler_output.json"))
}

// handleBusinessOpMetric serves the business's merged op_metric JSON for a
// given cycle (?ts=), latest when omitted.
func (c *Center) handleBusinessOpMetric(w http.ResponseWriter, r *http.Request) {
	dir := c.resultDir(r.PathValue("name"), r.URL.Query().Get("ts"))
	if dir == "" {
		http.Error(w, "no result yet", http.StatusNotFound)
		return
	}
	http.ServeFile(w, r, filepath.Join(dir, "op_metric.json"))
}

// handleBusinessTrigger immediately triggers one business's detection round.
func (c *Center) handleBusinessTrigger(w http.ResponseWriter, r *http.Request) {
	c.mu.Lock()
	b := c.biz[r.PathValue("name")]
	if b == nil {
		c.mu.Unlock()
		http.Error(w, "business not found", http.StatusNotFound)
		return
	}
	if b.Paused {
		c.mu.Unlock()
		http.Error(w, "business is paused", http.StatusConflict)
		return
	}
	if b.triggering {
		c.mu.Unlock()
		http.Error(w, "业务正在等待守护进程回传 op_metric，请稍后再触发", http.StatusConflict)
		return
	}
	ready := c.businessReadyLocked(b)
	c.mu.Unlock()
	if !ready {
		http.Error(w, "not all daemons healthy", http.StatusConflict)
		return
	}
	go c.triggerBusiness(b)
	writeJSON(w, map[string]string{"status": "triggered"})
}

// handleBusinessPause pauses a business's scheduling.
func (c *Center) handleBusinessPause(w http.ResponseWriter, r *http.Request) {
	c.mu.Lock()
	defer c.mu.Unlock()
	b := c.biz[r.PathValue("name")]
	if b == nil {
		http.Error(w, "business not found", http.StatusNotFound)
		return
	}
	b.Paused = true
	c.save()
	writeJSON(w, map[string]string{"status": "paused"})
}

// handleBusinessStart resumes a business's scheduling.
func (c *Center) handleBusinessStart(w http.ResponseWriter, r *http.Request) {
	c.mu.Lock()
	defer c.mu.Unlock()
	b := c.biz[r.PathValue("name")]
	if b == nil {
		http.Error(w, "business not found", http.StatusNotFound)
		return
	}
	b.Paused = false
	b.nextTrigger = time.Now().Add(c.businessInterval(b))
	c.save()
	writeJSON(w, map[string]string{"status": "started"})
}

// handleBusinessInterval updates a business's trigger period.
func (c *Center) handleBusinessInterval(w http.ResponseWriter, r *http.Request) {
	var req struct {
		IntervalSec int64 `json:"interval_sec"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.IntervalSec <= 0 {
		http.Error(w, `invalid body: {"interval_sec": 600}`, http.StatusBadRequest)
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	b := c.biz[r.PathValue("name")]
	if b == nil {
		http.Error(w, "business not found", http.StatusNotFound)
		return
	}
	b.IntervalSec = req.IntervalSec
	b.nextTrigger = time.Now().Add(time.Duration(req.IntervalSec) * time.Second)
	c.save()
	writeJSON(w, map[string]any{"interval_sec": req.IntervalSec})
}

// handleBusinessThresholds updates a business's merged-detection thresholds.
// Only affects future detection rounds; past results are untouched.
func (c *Center) handleBusinessThresholds(w http.ResponseWriter, r *http.Request) {
	var req config.Thresholds
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, `invalid body: {"cal","cpu","bubble_ns","comm_threshold","comm_min_count","comm_count_floor"}`, http.StatusBadRequest)
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	b := c.biz[r.PathValue("name")]
	if b == nil {
		http.Error(w, "business not found", http.StatusNotFound)
		return
	}
	b.Thresholds = req.Normalized()
	c.save()
	writeJSON(w, b.Thresholds)
}

// handleSetVLLMMetrics attaches a vllm /metrics endpoint URL to a business.
// Replaces any existing URL and resets the collected series.
func (c *Center) handleSetVLLMMetrics(w http.ResponseWriter, r *http.Request) {
	var req struct {
		URL string `json:"url"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, `invalid body: {"url"}`, http.StatusBadRequest)
		return
	}
	req.URL = strings.TrimSpace(req.URL)
	if !strings.HasPrefix(req.URL, "http://") && !strings.HasPrefix(req.URL, "https://") {
		http.Error(w, `url 需以 http:// 或 https:// 开头`, http.StatusBadRequest)
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	b := c.biz[r.PathValue("name")]
	if b == nil {
		http.Error(w, "business not found", http.StatusNotFound)
		return
	}
	b.VLLMMetrics = req.URL
	c.save()
	c.metrics.clear(b.Name)
	// Scrape immediately so the chart starts populating without waiting for the
	// next 20s tick (this first scrape only seeds the cumulative baseline).
	go func() {
		c.metrics.scrape(&http.Client{Timeout: metricsTimeout}, b.Name, req.URL)
	}()
	writeJSON(w, map[string]string{"vllm_metrics": req.URL})
}

// handleUnsetVLLMMetrics removes a business's vllm /metrics endpoint.
func (c *Center) handleUnsetVLLMMetrics(w http.ResponseWriter, r *http.Request) {
	c.mu.Lock()
	defer c.mu.Unlock()
	b := c.biz[r.PathValue("name")]
	if b == nil {
		http.Error(w, "business not found", http.StatusNotFound)
		return
	}
	b.VLLMMetrics = ""
	c.save()
	c.metrics.clear(b.Name)
	writeJSON(w, map[string]string{"status": "removed"})
}

// handleBusinessMetrics returns the collected TTFT/TPOT time series (per
// engine) for the business's vllm endpoint.
func (c *Center) handleBusinessMetrics(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	c.mu.Lock()
	_, ok := c.biz[name]
	c.mu.Unlock()
	if !ok {
		http.Error(w, "business not found", http.StatusNotFound)
		return
	}
	writeJSON(w, c.metrics.snapshot(name))
}

// tryMatch matches a daemon in a goroutine (best-effort on add).
func (c *Center) tryMatch(b *Business, d *Daemon) {
	c.mu.Lock()
	err := c.matchDaemonLocked(b, d)
	c.mu.Unlock()
	if err != nil {
		c.logf("business %s daemon %s match: %v", b.Name, d.Addr(), err)
	}
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_ = json.NewEncoder(w).Encode(v)
}
