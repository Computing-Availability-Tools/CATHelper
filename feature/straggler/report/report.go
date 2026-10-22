// Package report generates a human-readable detection report with bar charts.
package report

import (
	"fmt"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/config"
	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/profiling/detector"
	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/utils"
)

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const (
	barChar     = "█"
	barMaxWidth = 40
	topN        = 30
	bottomN     = 5
	reportSep   = 80
)

// ---------------------------------------------------------------------------
// WriteReport — public entry point
// ---------------------------------------------------------------------------

// WriteReport generates a text report and writes it to
// analysis_result/detection_report.log under outputDir.
// Returns the path to the written report file.
func WriteReport(
	stepData map[string]map[int]float64,
	parallels map[string][][]int,
	validRanks []int,
	outputDir string,
	detectionResult map[string]map[string]float64,
	inputPath string,
) string {
	report := GenerateReport(stepData, parallels, validRanks, detectionResult, inputPath)

	outDir := filepath.Join(outputDir, "analysis_result")
	os.MkdirAll(outDir, 0755)
	outPath := filepath.Join(outDir, "detection_report.log")
	if err := os.WriteFile(outPath, []byte(report), 0644); err != nil {
		fmt.Fprintf(os.Stderr, "[REPORT] Failed to write report: %v\n", err)
		return ""
	}
	fmt.Fprintf(os.Stderr, "[REPORT] Report written to %s\n", outPath)
	return outPath
}

// GenerateReport builds the full report text and returns it as a string.
func GenerateReport(
	stepData map[string]map[int]float64,
	parallels map[string][][]int,
	validRanks []int,
	detectionResult map[string]map[string]float64,
	inputPath string,
) string {
	var sb strings.Builder

	// Degraded mode: no parallel topology (group names not registered). Only
	// cal has input data — comm/CPU/bubble are not judged and must not be
	// reported as "normal" just because their data is absent.
	calOnly := len(parallels) == 0

	// Header.
	sb.WriteString(sepLine("慢节点检测报告", reportSep))
	sb.WriteString(fmt.Sprintf("\n  数据来源: %s\n", inputPath))
	sb.WriteString(fmt.Sprintf("  生成时间: %s\n", time.Now().Format("2006-01-02 15:04:05")))
	sb.WriteString(fmt.Sprintf("  有效 Rank 数: %d\n\n", len(validRanks)))

	// Topology summary.
	sb.WriteString(sepLine("并行域拓扑", 60))
	if calOnly {
		sb.WriteString("  未注册组名,无并行拓扑(检测降级为仅慢计算)\n")
	} else {
		for domain, groups := range parallels {
			sb.WriteString(fmt.Sprintf("  %s: %d 个 Group\n", domain, len(groups)))
		}
	}
	sb.WriteString("\n")

	// Detection summary.
	sb.WriteString(sepLine("检测结果摘要", reportSep))
	sb.WriteString(detectionSummary(detectionResult, validRanks, calOnly))
	sb.WriteString("\n")

	// ZP_Kernel section.
	if kernelData, ok := stepData["ZP_Kernel"]; ok {
		abnormal := abnormalSingleRanks(detectionResult["cal"])
		sb.WriteString(metricSection("计算类算子耗时排序", kernelData, abnormal))
		sb.WriteString("\n")
	}

	// ZP_Host section. Host time is a NODE-level metric: ranks of one physical
	// node share the host (detection smooths them by hostUid), so ranking raw
	// per-rank values is meaningless. The section renders per-node aggregates
	// and only appears with ≥2 physical nodes — the same gate the detection
	// uses — otherwise cross-node comparison is impossible.
	if !calOnly && utils.PhysicalNodeCount() >= 2 {
		if hostData, ok := stepData["ZP_Host"]; ok {
			sb.WriteString(hostSection(hostData, validRanks))
			sb.WriteString("\n")
		}
	}

	// Communication sections (skipped in degraded mode: no input data).
	// Slow communication is compared PER GROUP, never per rank, so there is
	// no per-rank "总通信时间" ranking — only the per-domain group tables.
	if !calOnly {
		for domain, groups := range parallels {
			if domain == "pp" || domain == "embd" {
				continue
			}
			colName := domain + "_Duration"
			if commData, ok := stepData[colName]; ok {
				abnormalGroups := abnormalCommGroups(detectionResult["comm"])
				sb.WriteString(commSection(domain, groups, commData, abnormalGroups))
				sb.WriteString("\n")
			}
		}
	}

	return sb.String()
}

// ---------------------------------------------------------------------------
// Section builders
// ---------------------------------------------------------------------------

func metricSection(metricName string, data map[int]float64, abnormalRanks map[int]bool) string {
	valid := filterValid(data)
	if len(valid) == 0 {
		return ""
	}

	// Sort by value descending.
	type kv struct {
		rank  int
		value float64
	}
	var sorted []kv
	for rank, v := range valid {
		sorted = append(sorted, kv{rank: rank, value: v})
	}
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].value > sorted[j].value })

	maxVal := sorted[0].value

	var sb strings.Builder
	sb.WriteString(sepLine(metricName, 80))

	top := sorted
	if len(sorted) > topN+bottomN {
		top = sorted[:topN]
	}
	var bottom []kv
	if len(sorted) > topN+bottomN {
		bottom = sorted[len(sorted)-bottomN:]
	}

	var values []float64
	for _, kv := range sorted {
		values = append(values, kv.value)
	}
	med := median(values)

	sb.WriteString(fmt.Sprintf("  Max: %s  Min: %s  Mean: %s  Median: %s\n\n",
		fmtNs(maxVal), fmtNs(sorted[len(sorted)-1].value),
		fmtNs(mean(values)), fmtNs(med)))

	sb.WriteString("  " + padEnd("#", 4) + "  " + padEnd("Rank", 6) + "  " + padStart("耗时", 12) + "  " + padStart("劣化指数", 10) + "  柱状图\n")
	sb.WriteString("  " + strings.Repeat("-", 4) + "  " + strings.Repeat("-", 6) + "  " + strings.Repeat("-", 12) + "  " + strings.Repeat("-", 10) + "  " + strings.Repeat("-", barMaxWidth) + "\n")

	printRank := func(idx int, kv kv) {
		deg := kv.value / valid[minValue(valid)]
		if deg == 0 {
			deg = 1
		}
		marker := ""
		if abnormalRanks[kv.rank] {
			marker = " ***"
		}
		sb.WriteString("  " + padEnd(strconv.Itoa(idx+1), 4) + "  " + padEnd(strconv.Itoa(kv.rank), 6) + "  " + padStart(fmtNs(kv.value), 12) + "  " + padStart(fmt.Sprintf("%.2fx", deg), 10) + "  " + bar(kv.value, maxVal) + marker + "\n")
	}

	for i, kv := range top {
		printRank(i, kv)
	}

	if len(sorted) > topN+bottomN {
		sb.WriteString(fmt.Sprintf("  ...  (省略 %d 个)\n", len(sorted)-topN-bottomN))
	}

	for i, kv := range bottom {
		printRank(len(sorted)-len(bottom)+i, kv)
	}

	return sb.String()
}

func commSection(domainName string, domainGroups [][]int, commData map[int]float64, abnormalGroups map[string]bool) string {
	var sb strings.Builder
	sb.WriteString(sepLine(domainName+" 通信域分组对比", 80))

	// Sort groups lexicographically.
	sortedGroups := make([][]int, len(domainGroups))
	for i, g := range domainGroups {
		sg := make([]int, len(g))
		copy(sg, g)
		sort.Ints(sg)
		sortedGroups[i] = sg
	}
	sort.Slice(sortedGroups, func(i, j int) bool {
		for k := 0; k < len(sortedGroups[i]) && k < len(sortedGroups[j]); k++ {
			if sortedGroups[i][k] != sortedGroups[j][k] {
				return sortedGroups[i][k] < sortedGroups[j][k]
			}
		}
		return len(sortedGroups[i]) < len(sortedGroups[j])
	})

	var maxMin float64
	var minOverall float64
	type groupStat struct {
		group    []int
		min      float64
		abnormal bool
	}
	var stats []groupStat

	for _, g := range sortedGroups {
		var vals []float64
		for _, r := range g {
			if v, ok := commData[r]; ok {
				vals = append(vals, v)
			}
		}
		if len(vals) == 0 {
			continue
		}
		sort.Float64s(vals)
		mn := vals[0]

		// Check if abnormal.
		key := joinInts(g, ",")
		ab := abnormalGroups[key]

		stats = append(stats, groupStat{group: g, min: mn, abnormal: ab})
		if mn > maxMin {
			maxMin = mn
		}
		if minOverall == 0 || mn < minOverall {
			minOverall = mn
		}
	}

	if maxMin == 0 {
		maxMin = 1
	}
	if minOverall == 0 {
		minOverall = 1
	}

	// Sort by cost descending (same as the compute ranking) so the slowest group
	// is on top, rather than leaving the lexicographic group order.
	sort.Slice(stats, func(i, j int) bool { return stats[i].min > stats[j].min })

	sb.WriteString("  " + padEnd("Group", 20) + "  " + padStart("耗时", 12) + "  " + padStart("劣化指数", 10) + "  柱状图\n")
	sb.WriteString("  " + strings.Repeat("-", 20) + "  " + strings.Repeat("-", 12) + "  " + strings.Repeat("-", 10) + "  " + strings.Repeat("-", barMaxWidth) + "\n")

	for _, st := range stats {
		marker := ""
		if st.abnormal {
			marker = " ***"
		}
		deg := st.min / minOverall
		if deg == 0 {
			deg = 1
		}
		groupLabel := "[" + joinInts(st.group, ", ") + "]"
		sb.WriteString("  " + padEnd(groupLabel, 20) + "  " + padStart(fmtNs(st.min), 12) + "  " + padStart(fmt.Sprintf("%.2fx", deg), 10) + "  " + bar(st.min, maxMin) + marker + "\n")
	}

	return sb.String()
}

// hostSection renders ZP_Host as a per-NODE table: ranks of one physical node
// share the host, so the meaningful comparison is across nodes, not across
// ranks. Each row is one physical node (hostUid from host_info_{N}.json),
// showing min/mean/max of its ranks' ZP_Host. Returns "" when fewer than two
// nodes have data (nothing to compare).
func hostSection(data map[int]float64, ranks []int) string {
	hostOf := detector.GetHostUidMapping(config.FilePath, ranks)

	nodeVals := make(map[string][]float64)
	for _, r := range ranks {
		v, ok := data[r]
		if !ok || v <= 0 {
			continue
		}
		h := hostOf[r]
		if h == "" {
			h = "unknown"
		}
		nodeVals[h] = append(nodeVals[h], v)
	}
	if len(nodeVals) < 2 {
		return ""
	}

	type nodeStat struct {
		name      string
		min, mean float64
		max       float64
	}
	hosts := make([]string, 0, len(nodeVals))
	for h := range nodeVals {
		hosts = append(hosts, h)
	}
	sort.Strings(hosts)

	var stats []nodeStat
	var maxMean float64
	for _, h := range hosts {
		vals := nodeVals[h]
		sort.Float64s(vals)
		st := nodeStat{name: h, min: vals[0], max: vals[len(vals)-1]}
		var sum float64
		for _, v := range vals {
			sum += v
		}
		st.mean = sum / float64(len(vals))
		stats = append(stats, st)
		if st.mean > maxMean {
			maxMean = st.mean
		}
	}
	if maxMean == 0 {
		maxMean = 1
	}

	var sb strings.Builder
	sb.WriteString(sepLine("ZP_Host 节点对比 (跨节点)", 80))
	sb.WriteString("  " + padEnd("Node", 24) + "  " + padStart("Min", 10) + "  " + padStart("Mean", 10) + "  " + padStart("Max", 10) + "  柱状图\n")
	sb.WriteString("  " + strings.Repeat("-", 24) + "  " + strings.Repeat("-", 10) + "  " + strings.Repeat("-", 10) + "  " + strings.Repeat("-", 10) + "  " + strings.Repeat("-", barMaxWidth) + "\n")
	for _, st := range stats {
		sb.WriteString("  " + padEnd(st.name, 24) + "  " + padStart(fmtNs(st.min), 10) + "  " + padStart(fmtNs(st.mean), 10) + "  " + padStart(fmtNs(st.max), 10) + "  " + bar(st.mean, maxMean) + "\n")
	}
	return sb.String()
}

func detectionSummary(
	detectionResult map[string]map[string]float64,
	validRanks []int,
	calOnly bool,
) string {
	var sb strings.Builder

	th := config.Current()
	sb.WriteString(fmt.Sprintf("  慢计算阈值: %.2f, 慢CPU阈值: %.2f, Bubble阈值: %.0fns, 慢通信阈值: %.2f\n\n",
		th.Cal, th.CPU, th.BubbleNs, th.CommThreshold))

	sb.WriteString("  " + padEnd("检测类型", 22) + "  " + padEnd("状态", 10) + "  " + padEnd("异常数", 10) + "  异常详情\n")
	sb.WriteString("  " + strings.Repeat("-", 22) + "  " + strings.Repeat("-", 10) + "  " + strings.Repeat("-", 10) + "  " + strings.Repeat("-", 30) + "\n")

	// Ordered categories. In degraded mode (no parallel topology) only cal has
	// input data; the other categories are omitted instead of being reported
	// as "normal" without data.
	categories := []struct {
		key, label string
	}{
		{"cal", "慢计算 (cal)"},
		{"comm", "慢通信 (comm)"},
		{"cpu", "慢CPU (cpu)"},
		{"npu_bubble", "Bubble (npu_bubble)"},
	}
	if calOnly {
		categories = categories[:1]
	}

	for _, cat := range categories {
		data := detectionResult[cat.key]
		status := "正常"
		count := 0
		var details []string

		if len(data) > 0 {
			status = "异常"
			count = len(data)
			// Build detail strings (truncated).
			for k, v := range data {
				details = append(details, fmt.Sprintf("%s: %.2fx", k, v))
				if len(details) >= 5 {
					break
				}
			}
		}

		detailStr := "-"
		if len(details) > 0 {
			detailStr = strings.Join(details, "; ")
			if len(data) > 5 {
				detailStr += fmt.Sprintf(" ... +%d more", len(data)-5)
			}
		}

		sb.WriteString("  " + padEnd(cat.label, 22) + "  " + padEnd(status, 10) + "  " + padEnd(strconv.Itoa(count), 10) + "  " + detailStr + "\n")
	}

	if calOnly {
		sb.WriteString(fmt.Sprintf("  注: 未注册组名,无并行拓扑 — 检测降级为仅慢计算;慢通信/慢CPU/Bubble 无数据,本次不做判定。\n"))
	}

	return sb.String()
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

func fmtNs(value float64) string {
	abs := math.Abs(value)
	switch {
	case abs >= 1e9:
		return fmt.Sprintf("%.2fs", value/1e9)
	case abs >= 1e6:
		return fmt.Sprintf("%.2fms", value/1e6)
	case abs >= 1e3:
		return fmt.Sprintf("%.2fus", value/1e3)
	default:
		return fmt.Sprintf("%.0fns", value)
	}
}

func filterValid(data map[int]float64) map[int]float64 {
	result := make(map[int]float64)
	for k, v := range data {
		if v > 0 && v != -99999.0 {
			result[k] = v
		}
	}
	return result
}

func bar(value, maxValue float64) string {
	if maxValue == 0 {
		return ""
	}
	width := int(math.Round(value / maxValue * float64(barMaxWidth)))
	if width > barMaxWidth {
		width = barMaxWidth
	}
	if width < 1 {
		width = 1
	}
	return strings.Repeat(barChar, width)
}

// dw returns s's terminal display width (CJK / fullwidth runes count as 2
// columns, others as 1). Table columns must be padded by this, not by fmt's
// %Ns (which counts runes), or headers mixing CJK and ASCII drift out of line.
func dw(s string) int {
	w := 0
	for _, r := range s {
		switch {
		case (r >= 0x1100 && r <= 0x115F) || // Hangul Jamo
			(r >= 0x2E80 && r <= 0xA4CF) || // CJK Radicals .. Yi
			(r >= 0xAC00 && r <= 0xD7A3) || // Hangul Syllables
			(r >= 0xF900 && r <= 0xFAFF) || // CJK Compatibility
			(r >= 0xFE30 && r <= 0xFE4F) || // CJK Compatibility Forms
			(r >= 0xFF00 && r <= 0xFF60) || // Fullwidth Forms
			(r >= 0xFFE0 && r <= 0xFFE6): // Fullwidth signs
			w += 2
		default:
			w++
		}
	}
	return w
}

// padEnd left-aligns s to width terminal columns.
func padEnd(s string, width int) string {
	if p := width - dw(s); p > 0 {
		return s + strings.Repeat(" ", p)
	}
	return s
}

// padStart right-aligns s to width terminal columns.
func padStart(s string, width int) string {
	if p := width - dw(s); p > 0 {
		return strings.Repeat(" ", p) + s
	}
	return s
}

func sepLine(title string, width int) string {
	prefix := strings.Repeat("=", (width-len(title)-2)/2)
	suffix := strings.Repeat("=", width-len(title)-2-len(prefix))
	return fmt.Sprintf("\n%s %s %s\n", prefix, title, suffix)
}

func mean(values []float64) float64 {
	if len(values) == 0 {
		return 0
	}
	var sum float64
	for _, v := range values {
		sum += v
	}
	return sum / float64(len(values))
}

func median(sortedVals []float64) float64 {
	if len(sortedVals) == 0 {
		return 0
	}
	n := len(sortedVals)
	if n%2 == 0 {
		return (sortedVals[n/2-1] + sortedVals[n/2]) / 2
	}
	return sortedVals[n/2]
}

func joinInts(ints []int, sep string) string {
	parts := make([]string, len(ints))
	for i, v := range ints {
		parts[i] = strconv.Itoa(v)
	}
	return strings.Join(parts, sep)
}

func minValue(data map[int]float64) int {
	var minKey int
	minSet := false
	var minVal float64
	for k, v := range data {
		if !minSet || v < minVal {
			minVal = v
			minKey = k
			minSet = true
		}
	}
	return minKey
}

func abnormalSingleRanks(data map[string]float64) map[int]bool {
	result := make(map[int]bool)
	for k := range data {
		if r, err := strconv.Atoi(k); err == nil {
			result[r] = true
		}
	}
	return result
}

func abnormalCommGroups(data map[string]float64) map[string]bool {
	result := make(map[string]bool)
	for k := range data {
		result[k] = true
	}
	return result
}
