package daemon

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// startDynolog spawns the dynolog collector subprocess (once, at daemon start).
// When the IPC port is already taken the process exits quickly — the daemon
// logs and reuses the existing instance, since dyno talks to it over IPC
// anyway. Returns the *exec.Cmd to hold for cleanup, or nil when reusing.
func startDynolog(bin string, logf func(format string, args ...any)) *exec.Cmd {
	cmd := exec.Command(bin, "--enable-ipc-monitor", "--certs-dir", "NO_CERTS")
	cmd.Stdout = os.Stderr
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		logf("dynolog start failed (%v) — reusing existing instance", err)
		return nil
	}
	go func() {
		if err := cmd.Wait(); err != nil {
			logf("dynolog exited: %v", err)
		}
	}()
	logf("dynolog started (pid %d)", cmd.Process.Pid)
	return cmd
}

// triggerCollection runs the dyno nputrace command that starts profiler capture
// on the matched vllm processes, and verifies it took effect via commandStatus.
func (d *Daemon) triggerCollection() error {
	args := []string{
		"--certs-dir", "NO_CERTS",
		"nputrace",
		"--start-step", "-1",
		"--iterations", strconv.Itoa(d.cfg.Iterations),
		"--activities", "NPU,CPU",
		"--profiler-level", "Level0",
		"--msprof-tx",
		"--export-type", "Db",
		"--log-file", d.cfg.ProfilerDir,
	}
	out, err := exec.Command(d.cfg.DynoBin, args...).CombinedOutput()
	// The collection verdict comes only from commandStatus in the response:
	// dyno's process exit code says nothing about whether data was captured, so
	// an effective response wins even when the process exited non-zero.
	resp, perr := parseDynoResponse(string(out))
	if perr != nil {
		if err != nil {
			return fmt.Errorf("dyno trigger: %v (stderr: %s)", err, strings.TrimSpace(string(out)))
		}
		return fmt.Errorf("dyno response: %v (stdout: %s)", perr, strings.TrimSpace(string(out)))
	}
	if resp.CommandStatus != "effective" {
		return fmt.Errorf("dyno commandStatus=%s, processesMatched=%v", resp.CommandStatus, resp.ProcessesMatched)
	}
	if len(resp.ProcessesMatched) == 0 {
		return fmt.Errorf("dyno processesMatched empty — no vllm process with MSMONITOR_USE_DAEMON=1")
	}
	return nil
}

// parseDynoResponse extracts the JSON object from dyno's stdout. The payload is
// shaped "response = {...}" and is wrapped in a preamble ("Security Warning: ...",
// "NpuTrace config = ...") plus trailing status lines ("Matched N processes",
// "Trace output files will be written to: ..."). The JSON object itself is the
// text between the first '{' and the last '}' of the whole output.
func parseDynoResponse(stdout string) (*dynoResponse, error) {
	start := strings.Index(stdout, "{")
	end := strings.LastIndex(stdout, "}")
	if start >= 0 && end > start {
		var r dynoResponse
		if err := json.Unmarshal([]byte(stdout[start:end+1]), &r); err == nil {
			return &r, nil
		}
	}
	// Fallback: the whole trimmed output is JSON (no preamble/trailing text).
	var r dynoResponse
	if err := json.Unmarshal([]byte(strings.TrimSpace(stdout)), &r); err != nil {
		return nil, err
	}
	return &r, nil
}

// noDBsErr builds the "no .db after analyse" error with a listing of the
// profiler root's top-level entries, so the on-disk layout can be validated
// during field testing (e.g. whether dyno wrote one subdir per rank).
func (d *Daemon) noDBsErr() error {
	var b strings.Builder
	entries, err := os.ReadDir(d.cfg.ProfilerDir)
	if err != nil {
		return fmt.Errorf("no ascend_pytorch_profiler_*.db found after analyse (scan %s failed: %v)", d.cfg.ProfilerDir, err)
	}
	for _, e := range entries {
		info, ierr := e.Info()
		if ierr != nil {
			continue
		}
		fmt.Fprintf(&b, "\n  %s  %s", info.ModTime().Format(time.RFC3339), e.Name())
	}
	return fmt.Errorf("no ascend_pytorch_profiler_*.db found after analyse under %s (top-level:%s)",
		d.cfg.ProfilerDir, b.String())
}

// runAnalyse converts the raw profiler dump into .db files via torch_npu's
// analyse. python (with torch_npu installed) must be on PATH.
func runAnalyse(profilerPath string, logf func(format string, args ...any)) error {
	code := fmt.Sprintf(
		"from torch_npu.profiler.profiler import analyse; analyse(profiler_path=%q, export_type=['db'])",
		profilerPath,
	)
	out, err := exec.Command("python", "-c", code).CombinedOutput()
	if err != nil {
		return fmt.Errorf("python analyse: %v (%s)", err, strings.TrimSpace(string(out)))
	}
	logf("python analyse done for %s", profilerPath)
	return nil
}

// findDBs walks dir recursively and returns all ascend_pytorch_profiler_*.db paths.
func findDBs(dir string) []string {
	var dbs []string
	_ = filepath.Walk(dir, func(path string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() {
			return nil
		}
		base := filepath.Base(path)
		if strings.HasPrefix(base, "ascend_pytorch_profiler_") && strings.HasSuffix(base, ".db") {
			dbs = append(dbs, path)
		}
		return nil
	})
	return dbs
}
