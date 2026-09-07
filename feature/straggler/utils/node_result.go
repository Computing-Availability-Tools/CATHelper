// Package utils provides result-writing and utility functions for the
// straggler detection system.
package utils

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"

	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/config"
)

// ---------------------------------------------------------------------------
// Node-based output types
// ---------------------------------------------------------------------------

// ScoreResult is a single anomaly score for one detection aspect.
type ScoreResult struct {
	Score float64 `json:"score"`
}

// NpuResult aggregates per-NPU anomalies (cal, npu_bubble). Only fields with
// anomalies are populated (omitempty).
type NpuResult struct {
	ID        int          `json:"id"`
	Cal       *ScoreResult `json:"cal,omitempty"`
	NPUBubble *ScoreResult `json:"npu_bubble,omitempty"`
}

// NodeResult aggregates anomalies of one physical node. CPU is node-level;
// npu lists only the NPUs with anomalies.
type NodeResult struct {
	Hostname string      `json:"hostname"`
	Npu      []NpuResult `json:"npu"`
	CPU      *ScoreResult `json:"cpu,omitempty"`
}

// NodeOutput is the profiler result structure: node-aggregated anomalies plus
// communication-domain results. It is emitted as the "profiler" section of the
// combined output JSON. With --debug-output, node_result includes every node
// (even normal ones) with their diagnostic scores.
type NodeOutput struct {
	NodeResult       []NodeResult                  `json:"node_result"`
	CommDomainResult map[string]map[string]float64 `json:"comm_domain_result"`
}

// ---------------------------------------------------------------------------
// BuildNodeResult — node-aggregated profiler output
// ---------------------------------------------------------------------------

// DebugInfo carries the --debug-output diagnostic scores so BuildNodeResult can
// include every node/communication group (normal ones too).
type DebugInfo struct {
	ValidRanks []int
	RankScores map[int]map[string]float64 // rank → {cal, cpu, npu_bubble}
	CommScores map[string]map[string]float64 // domain → {groupKey: ratio}
}

// nodeAccumulator builds up one node's entries while scanning results.
type nodeAccumulator struct {
	hostname string
	npus     map[int]*NpuResult
	cpu      float64
	hasCPU   bool
}

// BuildNodeResult aggregates the profiler result into the node-based structure:
// per-NPU anomalies (cal/npu_bubble) and node-level cpu are grouped by physical
// node (hostname from HOST_INFO.hostName, NPU id from NPU_INFO.id), and
// communication results are grouped by domain name. It prints the per-category
// stdout summary and returns the NodeOutput for the caller to embed into the
// combined output; it does NOT write any file itself.
//
// With debug != nil (--debug-output), every valid rank's node is included and
// each NPU's cal / npu_bubble / cpu scores (plus all comm groups) are filled
// from the diagnostic scores even when not flagged — normal data shows its
// diagnostic scores too.
func BuildNodeResult(finalResult map[string]map[string]float64, parallels map[string][][]int, debug *DebugInfo) (*NodeOutput, error) {
	includeAll := debug != nil
	// Degraded mode: no parallel topology (group names not registered) — only
	// cal has input data; comm/CPU/bubble are not judged and must not be
	// reported as "normal" just because their data is absent.
	calOnly := len(parallels) == 0
	var metaRanks []int
	if includeAll {
		metaRanks = debug.ValidRanks
	} else {
		seen := make(map[int]bool)
		for _, cat := range []string{"cal", "cpu", "npu_bubble"} {
			for rankStr := range finalResult[cat] {
				if r, err := strconv.Atoi(rankStr); err == nil && !seen[r] {
					seen[r] = true
					metaRanks = append(metaRanks, r)
				}
			}
		}
	}
	meta := loadRankMeta(metaRanks)
	nodes := make(map[string]*nodeAccumulator)

	// cal / npu_bubble: per rank → node.npu[id].
	for _, cat := range []string{"cal", "npu_bubble"} {
		for rankStr, score := range finalResult[cat] {
			rank, err := strconv.Atoi(rankStr)
			if err != nil {
				continue
			}
			m, ok := meta[rank]
			if !ok || m.hostname == "" {
				continue
			}
			acc := ensureNodeAcc(nodes, m.hostname)
			npu := ensureNpuAcc(acc, m.npuID)
			if cat == "cal" {
				npu.Cal = &ScoreResult{Score: score}
			} else {
				npu.NPUBubble = &ScoreResult{Score: score}
			}
		}
	}

	// cpu: per rank → node-level score (all ranks of a slow node share the
	// trimmed-mean value, so any of them gives the node's score).
	for rankStr, score := range finalResult["cpu"] {
		rank, err := strconv.Atoi(rankStr)
		if err != nil {
			continue
		}
		m, ok := meta[rank]
		if !ok || m.hostname == "" {
			continue
		}
		acc := ensureNodeAcc(nodes, m.hostname)
		acc.cpu = score
		acc.hasCPU = true
	}

	// Debug: include every valid rank's node and fill normal ranks' scores from
	// the diagnostic ratios (anomalous scores above take precedence).
	if includeAll {
		for _, rank := range debug.ValidRanks {
			m, ok := meta[rank]
			if !ok || m.hostname == "" {
				continue
			}
			acc := ensureNodeAcc(nodes, m.hostname)
			npu := ensureNpuAcc(acc, m.npuID)
			sc, ok := debug.RankScores[rank]
			if !ok {
				continue
			}
			if npu.Cal == nil && sc["cal"] > 0 {
				npu.Cal = &ScoreResult{Score: sc["cal"]}
			}
			if npu.NPUBubble == nil && sc["npu_bubble"] > 0 {
				npu.NPUBubble = &ScoreResult{Score: sc["npu_bubble"]}
			}
			if !acc.hasCPU && sc["cpu"] > acc.cpu {
				acc.cpu = sc["cpu"]
				acc.hasCPU = true
			}
		}
	}

	// comm_domain_result: group slow communication groups by domain name.
	commDomains := make(map[string]map[string]float64)
	for groupKey, score := range finalResult["comm"] {
		ranks := stringToRanks(groupKey)
		domain := findDomainForRanks(ranks, parallels)
		if domain == "" {
			domain = "[" + groupKey + "]"
		}
		if commDomains[domain] == nil {
			commDomains[domain] = make(map[string]float64)
		}
		commDomains[domain][groupKey] = score
	}

	// Debug: merge all communication groups' ratios (anomalous scores above take
	// precedence) so normal groups show their score too.
	if includeAll && debug.CommScores != nil {
		for domain, groups := range debug.CommScores {
			if commDomains[domain] == nil {
				commDomains[domain] = make(map[string]float64)
			}
			for groupKey, score := range groups {
				if _, exists := commDomains[domain][groupKey]; !exists {
					commDomains[domain][groupKey] = score
				}
			}
		}
	}

	// Build node_result (sorted by hostname, npu by id for determinism).
	hostnames := sortedNodeHosts(nodes)
	nodeResults := make([]NodeResult, 0, len(hostnames))
	for _, hn := range hostnames {
		acc := nodes[hn]
		nr := NodeResult{Hostname: hn}
		if acc.hasCPU {
			nr.CPU = &ScoreResult{Score: acc.cpu}
		}
		for _, id := range sortedNpuIDs(acc.npus) {
			nr.Npu = append(nr.Npu, *acc.npus[id])
		}
		nodeResults = append(nodeResults, nr)
	}

	printNodeSummary(finalResult, calOnly)
	return &NodeOutput{NodeResult: nodeResults, CommDomainResult: commDomains}, nil
}

// ---------------------------------------------------------------------------
// Rank metadata (hostname + NPU id from op_metric intermediates)
// ---------------------------------------------------------------------------

type rankMeta struct {
	hostname string
	npuID    int
}

// loadRankMeta reads host_info_{N}.json (hostName) and npu_info_{N}.json (id)
// for the given ranks.
func loadRankMeta(ranks []int) map[int]rankMeta {
	meta := make(map[int]rankMeta)
	seen := make(map[int]bool)
	for _, rank := range ranks {
		if seen[rank] {
			continue
		}
		seen[rank] = true
		meta[rank] = readRankMeta(rank)
	}
	return meta
}

func readRankMeta(rank int) rankMeta {
	var m rankMeta
	metricDir := filepath.Join(config.FilePath, "op_metric")

	if raw, err := os.ReadFile(filepath.Join(metricDir, "host_info_"+strconv.Itoa(rank)+".json")); err == nil {
		var hi struct {
			HostUid  string `json:"hostUid"`
			HostName string `json:"hostName"`
		}
		if json.Unmarshal(raw, &hi) == nil {
			m.hostname = hi.HostName
			if m.hostname == "" {
				m.hostname = hi.HostUid // fallback to the physical-node id
			}
		}
	}
	if raw, err := os.ReadFile(filepath.Join(metricDir, "npu_info_"+strconv.Itoa(rank)+".json")); err == nil {
		var ni struct {
			ID int `json:"id"`
		}
		if json.Unmarshal(raw, &ni) == nil {
			m.npuID = ni.ID
		}
	}
	return m
}

// ---------------------------------------------------------------------------
// Accumulator helpers
// ---------------------------------------------------------------------------

func ensureNodeAcc(nodes map[string]*nodeAccumulator, hostname string) *nodeAccumulator {
	acc, ok := nodes[hostname]
	if !ok {
		acc = &nodeAccumulator{hostname: hostname, npus: make(map[int]*NpuResult)}
		nodes[hostname] = acc
	}
	return acc
}

func ensureNpuAcc(acc *nodeAccumulator, id int) *NpuResult {
	npu, ok := acc.npus[id]
	if !ok {
		npu = &NpuResult{ID: id}
		acc.npus[id] = npu
	}
	return npu
}

func sortedNodeHosts(nodes map[string]*nodeAccumulator) []string {
	keys := make([]string, 0, len(nodes))
	for k := range nodes {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

func sortedNpuIDs(npus map[int]*NpuResult) []int {
	ids := make([]int, 0, len(npus))
	for id := range npus {
		ids = append(ids, id)
	}
	sort.Ints(ids)
	return ids
}

// ---------------------------------------------------------------------------
// Print helpers
// ---------------------------------------------------------------------------

func printNodeSummary(finalResult map[string]map[string]float64, calOnly bool) {
	// Slow-CPU needs ≥2 physical nodes to be meaningful: hostUid-based trimming
	// collapses a single node's ranks to identical values, so no straggler can
	// be found. Skip its line entirely in that case.
	cpuDetectable := PhysicalNodeCount() >= 2

	// Degraded mode (no parallel topology, cal-only): state it explicitly and
	// only report cal — the other categories have no input data, so "无异常"
	// would be misleading.
	if calOnly {
		fmt.Printf("检测已降级为仅慢计算 (cal): 未注册组名,无并行拓扑;慢通信/慢CPU/Bubble 无数据,不做判定\n")
	}

	categories := []struct {
		key, label string
		skip       bool
	}{
		{"cal", "慢计算 (cal)", false},
		{"comm", "慢通信 (comm)", calOnly},
		{"cpu", "慢CPU (cpu)", calOnly || !cpuDetectable},
		{"npu_bubble", "Bubble (npu_bubble)", calOnly},
	}

	for _, cat := range categories {
		if cat.skip {
			continue
		}
		data := finalResult[cat.key]
		if len(data) == 0 {
			fmt.Printf("%s: 无异常\n", cat.label)
			continue
		}
		keys := make([]string, 0, len(data))
		for k := range data {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		var parts []string
		for _, k := range keys {
			parts = append(parts, fmt.Sprintf("%s: %.2fx", k, data[k]))
		}
		fmt.Printf("%s: 异常 (%d) %s\n", cat.label, len(data), strings.Join(parts, "; "))
	}
}

// PhysicalNodeCount returns the number of distinct physical nodes, read from
// op_metric/host_info_{N}.json (hostName, falling back to hostUid). It scans
// ALL rank metadata files, not just anomalous ones, so slow-CPU detectability
// (needs ≥2 nodes) and the report's cross-node sections are judged on the
// whole system.
func PhysicalNodeCount() int {
	metricDir := filepath.Join(config.FilePath, "op_metric")
	entries, err := os.ReadDir(metricDir)
	if err != nil {
		return 0
	}
	hosts := make(map[string]bool)
	for _, e := range entries {
		name := e.Name()
		if !strings.HasPrefix(name, "host_info_") || !strings.HasSuffix(name, ".json") {
			continue
		}
		if raw, err := os.ReadFile(filepath.Join(metricDir, name)); err == nil {
			var hi struct {
				HostUid  string `json:"hostUid"`
				HostName string `json:"hostName"`
			}
			if json.Unmarshal(raw, &hi) == nil {
				h := hi.HostName
				if h == "" {
					h = hi.HostUid
				}
				if h != "" {
					hosts[h] = true
				}
			}
		}
	}
	return len(hosts)
}
