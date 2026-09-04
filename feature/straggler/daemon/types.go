// Package daemon implements the resident daemon mode: periodically triggering
// profiler collection (dynolog/dyno), converting and analysing the data, and
// exposing results + control through HTTP. It shares the detection pipeline
// with the one-shot mode via an injected DetectFunc (see main.detectFromParsedData).
package daemon

import (
	"time"

	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/resource"
	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/utils"
)

// Config holds the daemon's run-time configuration (the --daemon CLI flags).
type Config struct {
	ProfilerDir string        // profiler 采集落盘根目录（--profiler-dir=，必填；传给 dyno 的 --log-file）
	KpiDir      string        // KPI 数据目录（--kpi-dir=，可选；空 = 每轮只跑 Profiler 检测）
	Interval    time.Duration // 循环周期，默认 600s
	Port        int           // HTTP 端口，默认 8080
	CollectWait time.Duration // dyno 触发成功后的等待秒数，默认 60s
	Iterations  int           // dyno nputrace 采集迭代数（--profiler-iterations=，默认 5）
	DynoBin     string        // dyno 可执行路径（build.sh 用 .deb 装到系统，启动时 PATH 解析）
	DynologBin  string        // dynolog 可执行路径（build.sh 用 .deb 装到系统，启动时 PATH 解析）
	Degradation float64       // 阈值参数透传（1+degradation / 1+degradation*5）
	DebugOutput bool          // --debug-output：结果含所有正常卡的诊断分
}

// DefaultConfig returns a Config with sensible defaults (CLI overrides).
func DefaultConfig() Config {
	return Config{
		Interval:    10 * time.Minute,
		Port:        8080,
		CollectWait: 60 * time.Second,
		Iterations:  5,
		Degradation: 0.3,
	}
}

// CycleResult is one cycle's metadata + result. The transient raw dump source
// is the --profiler-dir root (dyno writes one master_<pid>_<ts>_ascend_pt
// subdir per rank under it); the root is removed at the end of the cycle and
// the small result artifacts live in ./daemon_results/<start>/. DumpDir is
// that result archive dir — the actual post-analysis drop location — NOT the
// profiler root (which is deleted at every cycle's end). Serialized (minus the
// heavy fields) into daemon_meta.json inside the archive dir as a durable
// record; the query API reads the in-memory store, not this file.
type CycleResult struct {
	ID         int                       `json:"id"`
	StartedAt  time.Time                 `json:"started_at"`
	FinishedAt time.Time                 `json:"finished_at"`
	DurationMs int64                     `json:"duration_ms"`
	DBs        int                       `json:"dbs"`
	DumpDir    string                    `json:"dump_dir"`
	JSONPath   string                    `json:"json_path,omitempty"`
	KPI        *resource.DetectionResult `json:"-"`
	KPIStatus  string                    `json:"kpi_status,omitempty"` // "ok" / "disabled..." / "skipped: ..." / "failed: ..."
	Result     *utils.NodeOutput         `json:"-"`
	Summary    map[string]int            `json:"summary"`
	Report     string                    `json:"-"`
	Error      string                    `json:"error,omitempty"`
}

// dynoResponse is the JSON snippet embedded in the dyno trigger command's
// stdout (shaped "response = {...}"); it decides whether collection took effect.
// ProcessesMatched holds the PIDs of the vllm processes that accepted the
// trigger (empty = no process with MSMONITOR_USE_DAEMON=1).
type dynoResponse struct {
	CommandStatus    string `json:"commandStatus"`    // "effective" / "ineffective"
	ProcessesMatched []int  `json:"processesMatched"` // matched process PIDs
}

// DetectFunc is the shared profiler detection pipeline (main.detectFromParsedData),
// injected into the daemon so both modes call one implementation.
type DetectFunc func(inputPath string, degradation float64, debugOutput bool) (*DetectResult, error)

// DetectResult is the outcome of the profiler pipeline: the node output for the
// combined JSON, per-category anomaly counts, and the text report.
type DetectResult struct {
	NodeOutput *utils.NodeOutput
	Summary    map[string]int // cal/comm/cpu/npu_bubble -> anomaly counts
	Report     string         // report.GenerateReport text
}

// CombinedOutput is the merged KPI + profiler result written as one JSON file
// (the "straggler_output.json" shape shared with one-shot mode).
type CombinedOutput struct {
	KPI      *resource.DetectionResult `json:"kpi,omitempty"`
	Profiler *utils.NodeOutput         `json:"profiler,omitempty"`
}

// statusResponse is the GET /status payload.
type statusResponse struct {
	State        string        `json:"state"`
	IntervalSec  int64         `json:"interval_sec"`
	ProfilerDir  string        `json:"profiler_dir"`
	KpiDir       string        `json:"kpi_dir"`
	CyclesTotal  int           `json:"cycles_total"`
	CyclesFailed int           `json:"cycles_failed"`
	LastCycle    *cycleSummary `json:"last_cycle,omitempty"`
	NextRunAt    *time.Time    `json:"next_run_at,omitempty"`
}

// cycleSummary is the compact per-cycle entry served by /status and /history
// (what a serialized CycleResult looks like without the heavy fields).
type cycleSummary struct {
	ID         int            `json:"id"`
	StartedAt  time.Time      `json:"started_at"`
	FinishedAt time.Time      `json:"finished_at"`
	DurationMs int64          `json:"duration_ms"`
	DBs        int            `json:"dbs"`
	DumpDir    string         `json:"dump_dir"`
	Summary    map[string]int `json:"summary"`
	KPIStatus  string         `json:"kpi_status,omitempty"`
	Error      string         `json:"error,omitempty"`
}

// historyResponse is the GET /straggler/results/history payload.
type historyResponse struct {
	Cycles []*cycleSummary `json:"cycles"`
}

// opMetricViewResponse is the aggregated op_metric view served by
// /straggler/op_metric/latest and /straggler/op_metric/{id}: ranks → parsed
// per-rank files.
type opMetricViewResponse struct {
	Cycle int                     `json:"cycle"`
	Dir   string                  `json:"dir"`
	Ranks map[string]opMetricRank `json:"ranks"`
}

// opMetricRank is one rank's three op_metric artifacts. group_info/host_info
// are the JSON files as parsed; global_rank is the CSV turned into a JSON
// object (single row) or array (multiple rows).
type opMetricRank struct {
	GroupInfo  map[string]any `json:"group_info"`
	HostInfo   map[string]any `json:"host_info"`
	GlobalRank any            `json:"global_rank"`
}
