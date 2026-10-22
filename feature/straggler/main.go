// Command slowNodeDetection is the straggler (slow-node) detection tool for
// AI training clusters. It reads Ascend PyTorch Profiler Level0 data (one
// SQLite .db file per NPU device), detects performance-degraded devices
// across four dimensions (compute, communication, CPU, NPU bubble), and
// outputs results as JSON and a human-readable text report.
//
// Optionally, a KPI resource CSV can be provided for lightweight NPU resource
// anomaly detection before the heavy Profiler analysis.
//
// Two modes:
//   - one-shot:  go run . path=/data/dir [--cal-threshold=1.3] [--cpu-threshold=2.5] [--kpi-path=/dir/of/kpi_csvs]
//   - daemon:    go run . --daemon --profiler-dir=/dir --kpi-dir=/dir [...]
//     The daemon periodically triggers profiler collection (dynolog/dyno),
//     converts and analyses the data, and exposes results + control over HTTP.
//
// Build:
//
//	bash build.sh && CGO_ENABLED=0 go build -o slowNodeDetection .
package main

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	_ "modernc.org/sqlite"

	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/center"
	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/config"
	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/daemon"
	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/profiling/dataparse"
	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/profiling/detector"
	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/report"
	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/resource"
	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/utils"
)

func main() {
	// 1. Parse CLI arguments.
	var inputPath string
	var kpiPath string
	var kpiJSONLDir string
	thresholds := config.DefaultThresholds() // independent detection thresholds (see config.Thresholds)
	spaceRatioThreshold := 0.0               // 0 = use the default SpaceRatioThreshold (2.0)
	debugOutput := false                     // --debug-output: include all normal+abnormal data (kpi.debug / profiler.debug) in straggler_output.json
	commFlat := false                        // --comm-flat: compute bandwidth from all ranks' op durations (no cross-rank alignment)

	// Daemon-mode flags.
	daemonMode := false
	daemonPort := 8080
	intervalSec := 600
	collectWait := 60
	profilerIterations := 1
	profilerDir := ""
	kpiDir := ""

	// Center-mode flags.
	centerMode := false
	centerPort := 8080
	centerDataDir := "center_data"
	centerIntervalSec := 600

	for _, arg := range os.Args[1:] {
		// Bare boolean flag (no "=value").
		if arg == "--debug-output" {
			debugOutput = true
			continue
		}
		if arg == "--comm-flat" {
			commFlat = true
			continue
		}
		if arg == "--daemon" {
			daemonMode = true
			continue
		}
		if arg == "--center" {
			centerMode = true
			continue
		}
		parts := strings.SplitN(arg, "=", 2)
		if len(parts) != 2 {
			continue
		}
		key, val := parts[0], parts[1]
		switch key {
		case "--debug-output":
			debugOutput = val == "true" || val == "1"
		case "--daemon-port":
			if parsed, err := strconv.Atoi(val); err == nil && parsed > 0 {
				daemonPort = parsed
			} else {
				fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] WARNING: invalid --daemon-port value, using default 8080\n")
			}
		case "--interval":
			if parsed, err := strconv.Atoi(val); err == nil && parsed >= 60 {
				intervalSec = parsed
			} else {
				fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] WARNING: invalid --interval value (>=60), using default 600\n")
			}
		case "--collect-wait":
			if parsed, err := strconv.Atoi(val); err == nil && parsed > 0 {
				collectWait = parsed
			} else {
				fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] WARNING: invalid --collect-wait value, using default 60\n")
			}
		case "--profiler-iterations":
			if parsed, err := strconv.Atoi(val); err == nil && parsed > 0 {
				profilerIterations = parsed
			} else {
				fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] WARNING: invalid --profiler-iterations value, using default 1\n")
			}
		case "--profiler-dir":
			profilerDir = val
		case "--kpi-dir":
			kpiDir = val
		case "--center-port":
			if parsed, err := strconv.Atoi(val); err == nil && parsed > 0 {
				centerPort = parsed
			} else {
				fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] WARNING: invalid --center-port value, using default 8080\n")
			}
		case "--center-data-dir":
			centerDataDir = val
		case "--center-interval":
			if parsed, err := strconv.Atoi(val); err == nil && parsed >= 60 {
				centerIntervalSec = parsed
			} else {
				fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] WARNING: invalid --center-interval value (>=60), using default 600\n")
			}
		case "path":
			inputPath = val
		case "--cal-threshold":
			if v, err := strconv.ParseFloat(val, 64); err == nil && v > 0 {
				thresholds.Cal = v
			} else {
				fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] WARNING: invalid --cal-threshold value, using default\n")
			}
		case "--cpu-threshold":
			if v, err := strconv.ParseFloat(val, 64); err == nil && v > 0 {
				thresholds.CPU = v
			} else {
				fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] WARNING: invalid --cpu-threshold value, using default\n")
			}
		case "--bubble-threshold-ns":
			if v, err := strconv.ParseFloat(val, 64); err == nil && v > 0 {
				thresholds.BubbleNs = v
			} else {
				fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] WARNING: invalid --bubble-threshold-ns value, using default\n")
			}
		case "--kpi-path":
			kpiPath = val
		case "--kpi-jsonl-dir":
			kpiJSONLDir = val
		case "--space-ratio-threshold":
			if parsed, err := strconv.ParseFloat(val, 64); err == nil && parsed > 0 {
				spaceRatioThreshold = parsed
			} else {
				fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] WARNING: invalid --space-ratio-threshold value, using default\n")
			}
		case "--comm-threshold":
			if parsed, err := strconv.ParseFloat(val, 64); err == nil && parsed > 1 {
				thresholds.CommThreshold = parsed
			} else {
				fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] WARNING: invalid --comm-threshold value (must be > 1), using default 1.3\n")
			}
		case "--comm-min-count":
			if parsed, err := strconv.Atoi(val); err == nil && parsed > 0 {
				thresholds.CommMinCount = parsed
			} else {
				fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] WARNING: invalid --comm-min-count value, using default 1000\n")
			}
		case "--comm-count-floor":
			if parsed, err := strconv.Atoi(val); err == nil && parsed > 0 {
				thresholds.CommCountFloor = parsed
			} else {
				fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] WARNING: invalid --comm-count-floor value, using default 10240\n")
			}
		}
	}

	// Slow-domain bandwidth mode + apply the active thresholds globally (one-shot
	// reads them directly; daemon/center re-apply their own per-scope values).
	config.SlowCommFlat = commFlat
	config.Apply(thresholds)

	// ─────────────────────────────────────────────────────────────────
	// Daemon mode: resident service (dynolog/dyno collection + HTTP).
	// ─────────────────────────────────────────────────────────────────
	if daemonMode {
		if profilerDir == "" {
			fmt.Fprintf(os.Stderr, "Usage: slowNodeDetection --daemon --profiler-dir=/dir [--kpi-dir=/dir] [--daemon-port=8080] [--interval=600] [--collect-wait=60] [--profiler-iterations=1]\n")
			fmt.Fprintf(os.Stderr, "ERROR: --daemon requires --profiler-dir (--kpi-dir is optional; omit to run profiler-only cycles)\n")
			os.Exit(1)
		}

		// dyno/dynolog are installed system-wide by build.sh (a .deb installed
		// via the host package manager); resolve them from PATH instead of
		// embedding them in the binary.
		dynoBin, err := exec.LookPath("dyno")
		if err != nil {
			fmt.Fprintf(os.Stderr, "ERROR: dyno not found in PATH (run build.sh to install it)\n")
			os.Exit(1)
		}
		dynologBin, err := exec.LookPath("dynolog")
		if err != nil {
			fmt.Fprintf(os.Stderr, "ERROR: dynolog not found in PATH (run build.sh to install it)\n")
			os.Exit(1)
		}

		cfg := daemon.DefaultConfig()
		cfg.ProfilerDir = profilerDir
		cfg.KpiDir = kpiDir
		cfg.Port = daemonPort
		cfg.Interval = time.Duration(intervalSec) * time.Second
		cfg.CollectWait = time.Duration(collectWait) * time.Second
		cfg.Iterations = profilerIterations
		cfg.DynoBin = dynoBin
		cfg.DynologBin = dynologBin
		cfg.Thresholds = thresholds
		cfg.DebugOutput = debugOutput

		d := daemon.New(cfg, detectFromParsedData)

		if kpiDir != "" {
			fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] === Daemon Mode (profiler=%s kpi=%s) ===\n", profilerDir, kpiDir)
		} else {
			fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] === Daemon Mode (profiler=%s, KPI detection disabled) ===\n", profilerDir)
		}
		ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
		defer stop()
		if err := d.Run(ctx); err != nil {
			fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] daemon failed: %v\n", err)
			os.Exit(1)
		}
		return
	}

	// ─────────────────────────────────────────────────────────────────
	// Center mode: manages multiple businesses (daemons) — matching,
	// health probing, merged detection, web console.
	// ─────────────────────────────────────────────────────────────────
	if centerMode {
		cfg := center.DefaultConfig()
		cfg.Port = centerPort
		cfg.DataDir = centerDataDir
		cfg.Interval = time.Duration(centerIntervalSec) * time.Second
		cfg.Thresholds = thresholds

		c := center.New(cfg)
		fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] === Center Mode (port=%d data=%s) ===\n", centerPort, centerDataDir)
		ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
		defer stop()
		if err := c.Run(ctx); err != nil {
			fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] center failed: %v\n", err)
			os.Exit(1)
		}
		return
	}

	// ─────────────────────────────────────────────────────────────────
	// One-shot mode: first line of defense is KPI resource detection
	// ─────────────────────────────────────────────────────────────────
	// KPI input: --kpi-jsonl-dir (CATMonitor straggler_output JSONL) takes
	// precedence over --kpi-path (legacy kpi_collect.sh CSV directory). Either is optional.
	kpiInput := kpiPath
	if kpiJSONLDir != "" {
		kpiInput = kpiJSONLDir
	}

	// No input at all → usage error before anything runs.
	if inputPath == "" && kpiInput == "" {
		fmt.Fprintf(os.Stderr, "Usage: slowNodeDetection path=/your/data/dir [--kpi-path=/dir/of/kpi_csvs | --kpi-jsonl-dir=/dir] [--cal-threshold=1.3] [--cpu-threshold=2.5] [--bubble-threshold-ns=5000] [--space-ratio-threshold=2.0] [--comm-threshold=1.3] [--comm-min-count=1000] [--comm-count-floor=10240] [--comm-flat]\n")
		fmt.Fprintf(os.Stderr, "ERROR: Missing required parameter: path=/your/data/dir (or a KPI input)\n")
		os.Exit(1)
	}

	var kpiResult *resource.DetectionResult
	var profilerOut *utils.NodeOutput

	if kpiInput != "" {
		kpiCfg := resource.DefaultDetectionConfig()
		kpiCfg.EnableDebug = debugOutput // --debug-output: kpi result includes all cards × metrics
		if spaceRatioThreshold > 0 {
			// Space ratio threshold is an independent knob; only override
			// the default (2.0) when --space-ratio-threshold is provided.
			kpiCfg.SpaceRatioThreshold = spaceRatioThreshold
		}

		fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] === KPI Resource Detection ===\n")
		var err error
		if kpiJSONLDir != "" {
			// Read all CATMonitor straggler_kpi_{date}.jsonl files in the directory.
			ts, rerr := resource.ReadKPIFiles(kpiJSONLDir)
			if rerr != nil {
				err = rerr
			} else {
				kpiResult, err = resource.RunDetectionFromData(ts, kpiJSONLDir, kpiCfg)
			}
		} else {
			// --kpi-path is a directory of per-node CSV files + node_config.json.
			kpiResult, err = resource.RunDetectionFromDir(kpiPath, kpiCfg)
		}
		if err != nil {
			fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] KPI detection failed: %v\n", err)
			if inputPath != "" {
				fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] Falling through to Profiler detection...\n")
			}
		} else {
			// KPI text report (stdout only; no file is written).
			fmt.Print(resource.WriteReport(kpiResult))

			// Cross-validation decision messages (the combined JSON is written at
			// the end of main, after the Profiler step, when this is the only
			// KPI result it still gets emitted under the "kpi" key).
			switch {
			case resource.HasAnomaly(kpiResult) && inputPath == "":
				fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] KPI detection found anomalies. Done.\n")
			case resource.HasAnomaly(kpiResult):
				fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] KPI found anomalies, proceeding to Profiler for cross-validation...\n")
			case inputPath != "":
				fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] KPI found no anomalies, falling back to Profiler...\n")
			}
		}
	}

	// ─────────────────────────────────────────────────────────────────
	// One-shot mode: second line of defense is Profiler detection
	// ─────────────────────────────────────────────────────────────────
	if inputPath != "" {
		// Validate required path.
		if info, err := os.Stat(inputPath); err != nil || !info.IsDir() {
			fmt.Fprintf(os.Stderr, "ERROR: Invalid directory: %s (err: %v)\n", inputPath, err)
			os.Exit(1)
		}

		fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] Input path: %s\n", inputPath)
		fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] Thresholds: cal=%.2f cpu=%.2f bubble=%.0fns commRatio=%.2f commMinCount=%d commCountFloor=%d flat=%v\n",
			thresholds.Cal, thresholds.CPU, thresholds.BubbleNs, thresholds.CommThreshold, thresholds.CommMinCount, thresholds.CommCountFloor, commFlat)

		// Data parsing: SQLite → CSV + JSON intermediates.
		fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] Starting data parsing...\n")
		dataparse.DataParsing(inputPath)

		// Global backfill pass: re-scan the .db files and write per-domain
		// (opType,count) bandwidth columns into the CSVs for the bandwidth-based
		// slow-communication detection that follows.
		fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] Backfilling slow-domain bandwidth columns...\n")
		if err := dataparse.BackfillSlowDomainBandwidth(inputPath); err != nil {
			fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] WARNING: slow-domain bandwidth backfill failed: %v (continuing with Duration-based columns)\n", err)
		}
		fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] Backfill done.\n")

		// Shared detection pipeline (steps 4-8); os.Exit on fatal conditions.
		detectResult, derr := detectFromParsedData(inputPath, debugOutput)
		if derr != nil {
			fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] FATAL: %v\n", derr)
			os.Exit(1)
		}
		// Keep the node output for the combined JSON below; detectResult also
		// carries the summary/report used by daemon mode only.
		profilerOut = detectResult.NodeOutput
	}

	// ─────────────────────────────────────────────────────────────────
	// Combined JSON output: one file in the running directory holding both the
	// KPI and Profiler results under the "kpi"/"profiler" keys. A section is
	// absent when that dimension did not run (e.g. KPI-only → only "kpi").
	// ─────────────────────────────────────────────────────────────────
	if kpiResult != nil || profilerOut != nil {
		const combinedPath = "straggler_output.json"
		if err := daemon.WriteCombinedJSON(0, "", kpiResult, profilerOut, combinedPath); err != nil {
			fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] Failed to write combined output: %v\n", err)
		} else {
			fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] Result written to %s\n", combinedPath)
		}
	}

	if inputPath != "" {
		fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] Detection complete.\n")
	}
}

// ---------------------------------------------------------------------------
// Shared profiler detection pipeline (one-shot and daemon both call this)
// ---------------------------------------------------------------------------

// detectFromParsedData runs the detection stage after the op_metric
// intermediates are ready (the one-shot mode's steps 4-8): parallel topology →
// step data snapshot → detection → node aggregation → text report. The caller
// is responsible for having applied the active thresholds (config.Apply); this
// function only sets config.FilePath and returns the node output, per-category
// anomaly counts, and the report text.
//
// The parsing stage (step 3) is NOT inside this function: one-shot calls
// dataparse.DataParsing (full rescan, os.Exit on zero files), while the daemon
// calls dataparse.StartProcess (error return, survives a bad dump).
func detectFromParsedData(inputPath string, debugOutput bool) (*daemon.DetectResult, error) {
	config.FilePath = inputPath
	th := config.Current()
	fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] Thresholds: cal=%.2f cpu=%.2f bubble=%.0fns commRatio=%.2f commMinCount=%d commCountFloor=%d\n",
		th.Cal, th.CPU, th.BubbleNs, th.CommThreshold, th.CommMinCount, th.CommCountFloor)

	// 4. Get parallel topology from group_info JSON files.
	parallels, validRanks := detector.GetCurDetectionInfo(inputPath)
	if len(validRanks) == 0 {
		return nil, fmt.Errorf("failed to get valid ranks")
	}
	if len(parallels) == 0 {
		fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] WARNING: no parallel topology (group names not registered), degrading to cal-only detection\n")
	}
	fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] Valid ranks: %d, Parallel domains: %d\n",
		len(validRanks), len(parallels))

	// 5. Get single-snapshot step data from CSV files.
	stepData := detector.GetCurJobLastStepData(validRanks)
	if len(stepData) == 0 {
		return nil, fmt.Errorf("no valid step data")
	}

	// 6. Run detection pipeline.
	result := detector.DelimitDetection(stepData, parallels, validRanks)
	if result == nil {
		return nil, fmt.Errorf("detection returned no results")
	}

	// 7. Build node-aggregated result (the JSON goes into the combined output).
	var profilerOut *utils.NodeOutput
	if debugOutput {
		debug := &utils.DebugInfo{
			ValidRanks: validRanks,
			RankScores: detector.DebugRankScores(stepData, validRanks),
		}
		profilerOut, _ = utils.BuildNodeResult(result, parallels, debug)
	} else {
		profilerOut, _ = utils.BuildNodeResult(result, parallels, nil)
	}

	// 8. Text report (written to <inputPath>/analysis_result/detection_report.log;
	//    the text is also returned so the daemon can serve it over HTTP).
	report.WriteReport(stepData, parallels, validRanks, inputPath, result, inputPath)
	reportText := report.GenerateReport(stepData, parallels, validRanks, result, inputPath)

	return &daemon.DetectResult{
		NodeOutput: profilerOut,
		Summary:    summarizeProfiler(result),
		Report:     reportText,
	}, nil
}

// summarizeProfiler counts anomalies per profiler category for the cycle
// summary (a flat map merged with per-metric KPI counts by the daemon). Units:
// cal = 卡, comm = 通信组, cpu = 物理节点数（同节点 rank 共享 host，按 hostUid
// 去重），npu_bubble = 卡。
func summarizeProfiler(result config.DegradationData) map[string]int {
	return map[string]int{
		"cal":        len(result["cal"]),
		"comm":       len(result["comm"]),
		"cpu":        countCPUNodes(result["cpu"]),
		"npu_bubble": len(result["npu_bubble"]),
	}
}

// countCPUNodes counts distinct physical nodes among the flagged CPU ranks.
// hostUid comes from host_info_{N}.json; ranks without hostUid (profiler data
// lacking HOST_INFO) each count as their own node so information is not lost.
func countCPUNodes(flagged map[string]float64) int {
	ranks := make([]int, 0, len(flagged))
	for k := range flagged {
		if r, err := strconv.Atoi(k); err == nil {
			ranks = append(ranks, r)
		}
	}
	hostOf := detector.GetHostUidMapping(config.FilePath, ranks)
	nodes := make(map[string]bool)
	for _, r := range ranks {
		h := hostOf[r]
		if h == "" {
			h = fmt.Sprintf("rank-%d", r) // unknown host: rank as its own node
		}
		nodes[h] = true
	}
	return len(nodes)
}
