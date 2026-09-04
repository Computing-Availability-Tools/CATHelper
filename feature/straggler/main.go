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
//   - one-shot:  go run . path=/data/dir [degradation=0.3] [--kpi-path=/dir/of/kpi_csvs]
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
	degradation := 0.3
	spaceRatioThreshold := 0.0 // 0 = use the default SpaceRatioThreshold (2.0)
	debugOutput := false       // --debug-output: include all normal+abnormal data (kpi.debug / profiler.debug) in straggler_output.json

	// Daemon-mode flags.
	daemonMode := false
	daemonPort := 8080
	intervalSec := 600
	collectWait := 60
	profilerIterations := 5
	profilerDir := ""
	kpiDir := ""

	for _, arg := range os.Args[1:] {
		// Bare boolean flag (no "=value").
		if arg == "--debug-output" {
			debugOutput = true
			continue
		}
		if arg == "--daemon" {
			daemonMode = true
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
				fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] WARNING: invalid --profiler-iterations value, using default 5\n")
			}
		case "--profiler-dir":
			profilerDir = val
		case "--kpi-dir":
			kpiDir = val
		case "path":
			inputPath = val
		case "degradation":
			if parsed, err := strconv.ParseFloat(val, 64); err == nil {
				if parsed < 0 {
					fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] WARNING: degradation < 0, using default 0.3\n")
				} else {
					if parsed > 1 {
						fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] WARNING: degradation > 1 may produce unexpected results\n")
					}
					degradation = parsed
				}
			} else {
				fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] WARNING: invalid degradation value, using default 0.3\n")
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
		}
	}

	// ─────────────────────────────────────────────────────────────────
	// Daemon mode: resident service (dynolog/dyno collection + HTTP).
	// ─────────────────────────────────────────────────────────────────
	if daemonMode {
		if profilerDir == "" {
			fmt.Fprintf(os.Stderr, "Usage: slowNodeDetection --daemon --profiler-dir=/dir [--kpi-dir=/dir] [--daemon-port=8080] [--interval=600] [--collect-wait=60] [--profiler-iterations=5]\n")
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
		cfg.Degradation = degradation
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
		fmt.Fprintf(os.Stderr, "Usage: slowNodeDetection path=/your/data/dir [degradation=0.3] [--kpi-path=/dir/of/kpi_csvs | --kpi-jsonl-dir=/dir] [--space-ratio-threshold=2.0]\n")
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
		fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] Degradation: %.2f\n", degradation)

		// Data parsing: SQLite → CSV + JSON intermediates.
		fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] Starting data parsing...\n")
		dataparse.DataParsing(inputPath)

		// Shared detection pipeline (steps 4-8); os.Exit on fatal conditions.
		detectResult, derr := detectFromParsedData(inputPath, degradation, debugOutput)
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
		if err := daemon.WriteCombinedJSON(kpiResult, profilerOut, combinedPath); err != nil {
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
// step data snapshot → detection → node aggregation → text report. It sets the
// config globals (FilePath / CalThreshold / CommThreshold) and returns the
// node output, per-category anomaly counts, and the report text.
//
// The parsing stage (step 3) is NOT inside this function: one-shot calls
// dataparse.DataParsing (full rescan, os.Exit on zero files), while the daemon
// calls dataparse.StartProcess (error return, survives a bad dump).
func detectFromParsedData(inputPath string, degradation float64, debugOutput bool) (*daemon.DetectResult, error) {
	config.FilePath = inputPath
	config.CalThreshold = 1 + degradation
	config.CommThreshold = 1 + degradation*5
	fmt.Fprintf(os.Stderr, "[SLOWNODE ALGO] CalThreshold: %.2f, CommThreshold: %.2f\n",
		config.CalThreshold, config.CommThreshold)

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
			CommScores: detector.DebugCommScores(stepData, parallels),
		}
		profilerOut, _ = utils.BuildNodeResult(result, parallels, debug)
	} else {
		profilerOut, _ = utils.BuildNodeResult(result, parallels, nil)
	}

	// 8. Text report (written to <inputPath>/analysis_result/detection_report.log;
	//    the text is also returned so the daemon can serve it over HTTP).
	report.WriteReport(stepData, parallels, validRanks, inputPath, result, inputPath, degradation)
	reportText := report.GenerateReport(stepData, parallels, validRanks, result, inputPath, degradation)

	return &daemon.DetectResult{
		NodeOutput: profilerOut,
		Summary:    summarizeProfiler(result),
		Report:     reportText,
	}, nil
}

// summarizeProfiler counts anomalies per profiler category for the cycle
// summary. Units: cal = 卡, comm = 通信组, cpu = 物理节点数（同节点 rank 共享
// host，按 hostUid 去重），npu_bubble = 卡。
func summarizeProfiler(result config.DegradationData) daemon.CycleSummary {
	return daemon.CycleSummary{
		Profiler: map[string]int{
			"cal":        len(result["cal"]),
			"comm":       len(result["comm"]),
			"cpu":        countCPUNodes(result["cpu"]),
			"npu_bubble": len(result["npu_bubble"]),
		},
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

