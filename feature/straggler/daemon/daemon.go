package daemon

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/profiling/dataparse"
	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/resource"
	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/utils"
)

// Daemon is the resident service: it runs one collect→convert→parse→analyse
// cycle per interval and exposes results + control over HTTP.
type Daemon struct {
	cfg    Config
	detect DetectFunc
	st     *store
	logf   func(format string, args ...any)

	mu            sync.Mutex
	state         string        // "running" | "paused"
	interval      time.Duration // current cycle period (POST /daemon/interval updates it)
	nextRun       time.Time     // when the next cycle starts (zero when paused)
	cycleID       int           // per-process id, starting from 1
	cycleInFlight bool
	timer         *time.Timer   // cycle timer; stopped while paused, re-armed by Start/Trigger
	dynolog       *exec.Cmd     // dynolog child to kill on shutdown (nil = reusing existing)
	stopOnce      sync.Once     // guards stopCh so POST /daemon/stop closes it exactly once
	stopCh        chan struct{} // closed by POST /daemon/stop to request graceful shutdown
	removeResults bool          // set by Stop(): delete all daemon_results/ on shutdown
}

// New creates a Daemon. detect is the shared profiler pipeline
// (main.detectFromParsedData); both daemon cycles and one-shot mode call it.
func New(cfg Config, detect DetectFunc) *Daemon {
	if cfg.Interval <= 0 {
		cfg.Interval = 10 * time.Minute
	}
	if cfg.CollectWait <= 0 {
		cfg.CollectWait = 60 * time.Second
	}
	if cfg.Port <= 0 {
		cfg.Port = 8080
	}
	return &Daemon{
		cfg:      cfg,
		detect:   detect,
		st:       newStore(),
		logf:     func(format string, args ...any) { fmt.Fprintf(os.Stderr, "[DAEMON] "+format+"\n", args...) },
		state:    "running",
		interval: cfg.Interval,
		stopCh:   make(chan struct{}),
	}
}

// Run starts the HTTP server and dynolog, then cycles on the interval ticker
// until ctx is cancelled (SIGINT/SIGTERM) or the HTTP server fails. The first
// cycle runs after the first interval tick, not at startup; use
// POST /daemon/trigger for an immediate cycle.
func (d *Daemon) Run(ctx context.Context) error {
	srv := d.httpServer()
	ln, err := net.Listen("tcp", fmt.Sprintf(":%d", d.cfg.Port))
	if err != nil {
		return fmt.Errorf("HTTP listen :%d: %w", d.cfg.Port, err)
	}
	srvErr := make(chan error, 1)
	go func() {
		if err := srv.Serve(ln); err != nil && err != http.ErrServerClosed {
			srvErr <- err
		}
	}()
	d.logf("HTTP server listening on :%d", d.cfg.Port)

	if d.cfg.DynologBin != "" {
		d.dynolog = startDynolog(d.cfg.DynologBin, d.logf)
	}
	if d.cfg.KpiDir == "" {
		d.logf("KPI detection disabled (no --kpi-dir): cycles run profiler-only")
	} else {
		// Pre-flight: surface the KPI data source at startup so a wrong
		// --kpi-dir (or a CATMonitor straggler_output plugin that is off) is
		// visible immediately instead of silently skipping every cycle.
		n := countKPIFiles(d.cfg.KpiDir)
		d.logf("KPI detection enabled: --kpi-dir=%s (%d straggler_kpi_*.jsonl file(s))", d.cfg.KpiDir, n)
		if n == 0 {
			d.logf("WARNING: no straggler_kpi_*.jsonl found in %s — check that --kpi-dir points at CATMonitor's straggler_output.data_dir (default /var/lib/catmonitor/straggler) and that the straggler_output plugin is enabled", d.cfg.KpiDir)
		}
	}

	// First cycle runs after the first interval tick, not immediately at
	// startup. nextRun is set so /status shows the scheduled first run; use
	// POST /daemon/trigger for an immediate cycle.
	d.mu.Lock()
	d.nextRun = time.Now().Add(d.interval)
	d.timer = time.NewTimer(d.interval)
	// The HTTP server starts before this point, so a Pause could already have
	// arrived; in that case the timer must not stay armed (Start() re-arms).
	if d.state == "paused" {
		d.nextRun = time.Time{}
		d.timer.Stop()
	}
	d.mu.Unlock()
	defer d.stopTimer()

	for {
		select {
		case <-ctx.Done():
			return d.shutdown(srv)
		case <-d.stopCh:
			return d.shutdown(srv)
		case err := <-srvErr:
			return err
		case <-d.timer.C:
			d.mu.Lock()
			if d.state == "running" {
				d.startCycle()
				// Re-anchor both the display and the actual timer to the next
				// interval. (startCycle already sets nextRun unless a cycle
				// was in flight and this tick was skipped.)
				d.nextRun = time.Now().Add(d.interval)
				d.timer.Reset(d.interval)
			} else {
				// Paused: stop the timer so it does not keep ticking through
				// the pause; Start() re-arms it from now+interval.
				d.stopTimer()
			}
			d.mu.Unlock()
		}
	}
}

// startCycle launches one cycle if none is in flight (single-flight: a long
// cycle skips the ticks that land while it runs). Caller holds d.mu.
func (d *Daemon) startCycle() {
	if d.cycleInFlight {
		return
	}
	d.cycleInFlight = true
	d.cycleID++
	id := d.cycleID
	d.nextRun = time.Now().Add(d.interval)
	go func() {
		d.runCycle(id)
		d.mu.Lock()
		d.cycleInFlight = false
		d.mu.Unlock()
	}()
}

// runCycle executes one collect→convert→parse→analyse pass and records the
// CycleResult (success or error) into the store.
func (d *Daemon) runCycle(id int) {
	cr := &CycleResult{ID: id, StartedAt: time.Now()}
	// This cycle's analysed results (combined JSON, meta, report) are written
	// to ./daemon_results/<start>/ — OUTSIDE the --profiler-dir root — and
	// that archive dir is the cycle's dump_dir. The --profiler-dir root is only
	// the transient raw-collection input: dyno writes one
	// master_<pid>_<ts>_ascend_pt subdir PER RANK directly under it (the root,
	// not any single rank subdir, is what analyse/parse/detect operate on), and
	// cleanupDump deletes the whole root at the end of every cycle — so it must
	// not be reported as where this cycle's data lives.
	archive := filepath.Join("daemon_results", cr.StartedAt.Format("20060102-150405"))
	cr.DumpDir = archive
	defer func() {
		cr.FinishedAt = time.Now()
		cr.DurationMs = cr.FinishedAt.Sub(cr.StartedAt).Milliseconds()
		// Results are stored in daemon_results/<start>/ during the cycle; the
		// whole --profiler-dir is removed at the end of every cycle, success or
		// failure. dyno re-creates the root on the next trigger.
		d.cleanupDump(cr)
		d.finishCycle(cr)
	}()

	// 1. Collect: dyno trigger -> verify commandStatus -> wait for the dump.
	if err := d.triggerCollection(); err != nil {
		cr.Error = err.Error()
		return
	}
	time.Sleep(d.cfg.CollectWait)
	root := d.cfg.ProfilerDir

	// 2. Convert the raw dump to .db (torch_npu analyse) over the whole root.
	if err := runAnalyse(root, d.logf); err != nil {
		cr.Error = err.Error()
		return
	}

	// 3. Discover the .db files (recursive across all rank subdirs).
	dbFiles := findDBs(root)
	if len(dbFiles) == 0 {
		cr.Error = d.noDBsErr().Error()
		return
	}
	cr.DBs = len(dbFiles)

	// 4. Parse (StartProcess — not DataParsing, which os.Exit's on zero files).
	// Reset the per-path sync.Once dedup table: it is a package-level global
	// keyed by absolute path, and cleanupDump removed --profiler-dir last
	// cycle, so without a reset the stale Once objects make this cycle's
	// group_info_/host_info_ JSON writes no-ops → topology lost → slow
	// comm/CPU/bubble detection silently drops. One-shot mode is unaffected.
	dataparse.ResetFileWriteOnce()
	if err := dataparse.StartProcess(dbFiles, root); err != nil {
		cr.Error = fmt.Sprintf("StartProcess: %v", err)
		return
	}

	// 5. KPI detection (--kpi-dir, JSONL). Status is recorded on the cycle so
	//    whether KPI ran (and its outcome) is visible in history: "ok" means
	//    detection executed; disabled/skipped/failed carry the reason.
	cr.KPI, cr.KPIStatus = d.detectKPI()

	// 6. Profiler detection (shared pipeline; sets config.FilePath internally).
	res, derr := d.detect(root, d.cfg.Degradation, d.cfg.DebugOutput)
	if derr != nil {
		cr.Error = fmt.Sprintf("profiler detection: %v", derr)
		return
	}
	cr.Result = res.NodeOutput
	cr.Summary = res.Summary
	// Merge the KPI anomaly counts (per metric) into the cycle summary so
	// history shows both dimensions; the kpi segment is absent when KPI
	// detection produced no result.
	if cr.KPI != nil {
		cr.Summary.KPI = kpiMetricCounts(cr.KPI, d.cfg.DebugOutput)
	}
	cr.Report = res.Report

	// 7. Write the combined result JSON + cycle meta into the per-cycle archive
	//    dir (cr.DumpDir = ./daemon_results/<start>/), OUTSIDE the raw dump
	//    root. Keeping results out of --profiler-dir is what lets the cycle's
	//    end delete the heavy profiler folder without touching the query data
	//    source.
	if err := os.MkdirAll(archive, 0o755); err != nil {
		cr.Error = fmt.Sprintf("mkdir archive: %v", err)
		return
	}
	jsonPath := filepath.Join(archive, "straggler_output.json")
	if err := WriteCombinedJSON(cr.KPI, cr.Result, jsonPath); err != nil {
		cr.Error = fmt.Sprintf("write result JSON: %v", err)
		return
	}
	cr.JSONPath = jsonPath
	if err := writeMeta(archive, cr); err != nil {
		d.logf("write daemon_meta.json: %v", err)
	}
	// Running-dir copy of the latest combined result (same shape as one-shot).
	if err := copyFile(jsonPath, filepath.Join(".", "straggler_output.json")); err != nil {
		d.logf("copy result to run dir: %v", err)
	}

	// 8. The text report is written inside the root (report.WriteReport derives
	//    its path from the input dir); copy it into the archive as a durable
	//    record. Best effort — cr.Report already holds the text in memory.
	if src := filepath.Join(root, "analysis_result", "detection_report.log"); fileExists(src) {
		if err := os.MkdirAll(filepath.Join(archive, "analysis_result"), 0o755); err != nil {
			d.logf("cycle %d copy report: %v", cr.ID, err)
		} else if err := copyFile(src, filepath.Join(archive, "analysis_result", "detection_report.log")); err != nil {
			d.logf("cycle %d copy report: %v", cr.ID, err)
		}
	}

	// 9. Copy op_metric/ into the archive dir as the durable detector-input
	//    snapshot (group_info_*.json, host_info_*.json, global_rank_*.csv).
	//    cleanupDump (deferred below) removes the whole --profiler-dir, so
	//    without this copy the per-rank topology + metric CSVs are gone every
	//    cycle. Best effort — detection already produced cr.Report/Result.
	srcOpMetric := filepath.Join(root, "op_metric")
	if fileExistsDir(srcOpMetric) {
		if err := copyDir(srcOpMetric, archive); err != nil {
			d.logf("cycle %d copy op_metric: %v", cr.ID, err)
		}
	}
}

// detectKPI reads the latest KPI data from --kpi-dir and runs the same
// resource detection as one-shot mode. It returns the result plus a status
// string surfaced in the cycle's history, so a disabled/skipped/failed KPI
// pass is visible instead of silently absent. The cycle itself continues
// (profiler still runs) when the directory is empty or detection fails.
func (d *Daemon) detectKPI() (*resource.DetectionResult, string) {
	if d.cfg.KpiDir == "" {
		return nil, "disabled (no --kpi-dir)"
	}
	ts, err := resource.ReadKPIFiles(d.cfg.KpiDir)
	if err != nil {
		d.logf("KPI read skipped: %v", err)
		return nil, fmt.Sprintf("skipped: %v", err)
	}
	kpiCfg := resource.DefaultDetectionConfig()
	kpiCfg.EnableDebug = d.cfg.DebugOutput
	res, err := resource.RunDetectionFromData(ts, d.cfg.KpiDir, kpiCfg)
	if err != nil {
		d.logf("KPI detection failed: %v", err)
		return nil, fmt.Sprintf("failed: %v", err)
	}
	return res, "ok"
}

// kpiMetricCounts builds the kpi sub-summary: per KPI metric, the number of
// anomalous cards (unit: 卡). Without --debug-output the result lists only
// anomalous cards; with debug every card is listed with an Abnormal flag, so
// only flagged cards are counted.
func kpiMetricCounts(kpi *resource.DetectionResult, debug bool) map[string]int {
	out := make(map[string]int)
	for _, ma := range kpi.Metrics {
		if !debug {
			out[string(ma.Metric)] = len(ma.Cards)
			continue
		}
		n := 0
		for _, c := range ma.Cards {
			if c.Abnormal {
				n++
			}
		}
		out[string(ma.Metric)] = n
	}
	return out
}

// countKPIFiles returns how many straggler_kpi_*.jsonl files ReadKPIFiles would
// consume for dir, mirroring its layout handling: with a node_config.json the
// files live inside the per-node subfolders it references; without one they are
// directly inside dir. Returns 0 when the dir is missing/empty or uses a
// different layout.
func countKPIFiles(dir string) int {
	// Optional node_config.json switches to the multi-node layout: folder →
	// {node, cards}; each folder holds that node's jsonl files.
	if raw, err := os.ReadFile(filepath.Join(dir, "node_config.json")); err == nil {
		var cfg map[string]struct {
			Node  string `json:"node"`
			Cards []int  `json:"cards"`
		}
		if json.Unmarshal(raw, &cfg) == nil && len(cfg) > 0 {
			n := 0
			for folder := range cfg {
				n += countKPIFilesIn(filepath.Join(dir, folder))
			}
			return n
		}
	}
	return countKPIFilesIn(dir)
}

func countKPIFilesIn(dir string) int {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return 0
	}
	n := 0
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || !strings.HasPrefix(name, "straggler_kpi_") || !strings.HasSuffix(name, ".jsonl") {
			continue
		}
		n++
	}
	return n
}

// finishCycle records a finished cycle in the store and logs its outcome.
func (d *Daemon) finishCycle(cr *CycleResult) {
	d.st.add(cr)
	d.logf("cycle %d finished: dbs=%d error=%q", cr.ID, cr.DBs, cr.Error)
}

// shutdown stops the HTTP server, waits for an in-flight cycle (max 10 min),
// and kills the dynolog child we spawned.
func (d *Daemon) shutdown(srv *http.Server) error {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
	defer cancel()
	_ = srv.Shutdown(ctx)

	deadline := time.Now().Add(10 * time.Minute)
	for {
		d.mu.Lock()
		inflight := d.cycleInFlight
		d.mu.Unlock()
		if !inflight {
			break
		}
		if time.Now().After(deadline) {
			d.logf("giving up waiting for in-flight cycle")
			break
		}
		time.Sleep(500 * time.Millisecond)
	}

	if d.dynolog != nil {
		_ = d.dynolog.Process.Kill()
		_, _ = d.dynolog.Process.Wait()
	}
	if d.removeResults {
		if err := os.RemoveAll("daemon_results"); err != nil {
			d.logf("remove daemon_results: %v", err)
		} else {
			d.logf("removed all daemon_results (dump_dir) files")
		}
	}
	d.logf("daemon stopped")
	return nil
}

// ---------------------------------------------------------------------------
// Control operations (called by the HTTP handlers; see server.go)
// ---------------------------------------------------------------------------

// Pause stops scheduling new cycles; an in-flight cycle finishes naturally.
// The cycle timer is stopped so the schedule does not keep ticking through
// the pause (Start() re-arms it from now+interval).
func (d *Daemon) Pause() {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.state == "running" {
		d.state = "paused"
		d.nextRun = time.Time{}
		d.stopTimer()
	}
}

// Start resumes the cycle loop and schedules the next run after the interval,
// re-arming the timer so the actual fire matches next_run_at.
func (d *Daemon) Start() {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.state == "paused" {
		d.state = "running"
		d.nextRun = time.Now().Add(d.interval)
		d.resetTimer()
	}
}

// SetInterval updates the cycle period, validating [60, 86400] seconds. The
// running timer is re-armed so the new period takes effect immediately.
func (d *Daemon) SetInterval(sec int64) error {
	if sec < 60 || sec > 86400 {
		return fmt.Errorf("interval_sec out of range [60, 86400]")
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	d.interval = time.Duration(sec) * time.Second
	if d.state == "running" {
		d.nextRun = time.Now().Add(d.interval)
		d.resetTimer()
	}
	return nil
}

// Trigger runs one cycle immediately; returns an error when paused or when a
// cycle is already in flight (HTTP 409). The timer is re-anchored so the next
// automatic cycle is exactly one interval after the manual one.
func (d *Daemon) Trigger() error {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.state != "running" {
		return fmt.Errorf("daemon is paused")
	}
	if d.cycleInFlight {
		return fmt.Errorf("a cycle is already running")
	}
	d.startCycle()
	d.resetTimer()
	return nil
}

// Stop requests a graceful shutdown of the daemon: Run's select observes the
// closed stopCh and runs shutdown (HTTP server close, in-flight cycle wait,
// dynolog child kill). Idempotent — repeated calls are no-ops. All archived
// result files under daemon_results/ (each cycle's dump_dir) are removed as
// part of the shutdown.
func (d *Daemon) Stop() {
	d.stopOnce.Do(func() {
		d.mu.Lock()
		d.removeResults = true
		d.mu.Unlock()
		close(d.stopCh)
	})
}

// stopTimer stops the cycle timer, draining any stale fire so a later Reset
// takes effect cleanly (Stop returning false means a value may be pending in
// C). Caller holds d.mu.
func (d *Daemon) stopTimer() {
	if d.timer == nil {
		return
	}
	if !d.timer.Stop() {
		select {
		case <-d.timer.C:
		default:
		}
	}
}

// resetTimer re-arms the cycle timer to fire after the current interval.
// Caller holds d.mu.
func (d *Daemon) resetTimer() {
	d.stopTimer()
	if d.timer != nil {
		d.timer.Reset(d.interval)
	}
}

// ---------------------------------------------------------------------------
// Result JSON helpers
// ---------------------------------------------------------------------------

// WriteCombinedJSON marshals the KPI + profiler result into one JSON file at
// path — the shared straggler_output.json shape ({"kpi": ..., "profiler": ...}).
func WriteCombinedJSON(kpi *resource.DetectionResult, profiler *utils.NodeOutput, path string) error {
	out := CombinedOutput{KPI: kpi, Profiler: profiler}
	data, err := json.MarshalIndent(out, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal combined output: %w", err)
	}
	if err := os.WriteFile(path, data, 0644); err != nil {
		return fmt.Errorf("write combined output: %w", err)
	}
	return nil
}

// writeMeta serializes the lightweight cycle metadata into the dump directory.
func writeMeta(dumpDir string, cr *CycleResult) error {
	data, err := json.MarshalIndent(cr, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(dumpDir, "daemon_meta.json"), data, 0644)
}

func copyFile(src, dst string) error {
	data, err := os.ReadFile(src)
	if err != nil {
		return err
	}
	return os.WriteFile(dst, data, 0644)
}

// copyDir recursively copies src into dst (dst/src's basename is created).
// Errors are collected per-file; a non-nil error is returned only when the
// top-level src cannot be walked — individual file failures do not abort the
// copy so a partial op_metric still survives.
func copyDir(src, dst string) error {
	info, err := os.Stat(src)
	if err != nil {
		return err
	}
	target := filepath.Join(dst, info.Name())
	if err := os.MkdirAll(target, 0o755); err != nil {
		return err
	}
	return filepath.Walk(src, func(path string, fi os.FileInfo, werr error) error {
		if werr != nil {
			return werr
		}
		rel, rerr := filepath.Rel(src, path)
		if rerr != nil {
			return rerr
		}
		out := filepath.Join(target, rel)
		if fi.IsDir() {
			return os.MkdirAll(out, fi.Mode())
		}
		return copyFile(path, out)
	})
}

// cleanupDump removes the entire --profiler-dir at the end of every cycle,
// success or failure. dyno re-creates the root on the next trigger, so nothing
// is lost and no stale dump can skew a later cycle's newest-dir pick. Result
// artifacts live in daemon_results/<start>/ (outside profiler-dir) and are
// untouched; RemoveAll on a missing root is a no-op, so this is safe even when
// the trigger never created a dump. Failures are logged, never fatal.
func (d *Daemon) cleanupDump(cr *CycleResult) {
	if err := os.RemoveAll(d.cfg.ProfilerDir); err != nil {
		d.logf("cycle %d cleanup failed: %v", cr.ID, err)
		return
	}
	d.logf("cycle %d cleaned: removed %s", cr.ID, d.cfg.ProfilerDir)
}
