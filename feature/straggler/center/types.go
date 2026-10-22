// Package center implements the center-node mode: a manager that controls
// multiple businesses (real training tasks). Each business maps to one or more
// daemons (each a slowNodeDetection --daemon); the center matches those daemons,
// triggers them on its own timer, receives their reported op_metric JSON, merges
// and re-runs detection across the whole business, and serves a web console.
package center

import (
	"fmt"
	"time"

	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/config"
	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/profiling/detector"
)

// Daemon is one daemon within a business. Key is persisted so a center restart
// can re-authenticate to an already-matched daemon (whose own key survives in
// memory); collectWait / health / reported op_metric remain runtime-only and
// are rebuilt on re-match.
type Daemon struct {
	IP   string `json:"ip"`
	Port int    `json:"port"`
	Key  string `json:"key,omitempty"` // per-daemon match secret, persisted across center restarts

	collectWait int64  // daemon's --collect-wait (seconds), from /daemon/match
	healthy     bool   // last healthz probe result
	failCount   int    // consecutive healthz failures
	matchState  string // "", "healthy", "unmatched", "other"

	lastOpMetric detector.OpMetric // most recent reported op_metric (in-memory)
	lastReportAt time.Time         // when the last report arrived
}

// Addr returns "ip:port".
func (d *Daemon) Addr() string { return fmt.Sprintf("%s:%d", d.IP, d.Port) }

// BaseURL returns "http://ip:port".
func (d *Daemon) BaseURL() string { return fmt.Sprintf("http://%s:%d", d.IP, d.Port) }

// Business is one managed training task covering 1..N daemons (globally unique
// ranks across them). Paused / cycle counters are persisted; nextTrigger is
// runtime-only.
type Business struct {
	Name         string            `json:"name"`
	IntervalSec  int64             `json:"interval_sec"`
	Daemons      []*Daemon         `json:"daemons"`
	Paused       bool              `json:"paused,omitempty"`
	CyclesTotal  int               `json:"cycles_total"`
	CyclesFailed int               `json:"cycles_failed"`
	VLLMMetrics  string            `json:"vllm_metrics,omitempty"` // vllm /metrics endpoint URL (one per business)
	Thresholds   config.Thresholds `json:"thresholds"`             // merged-detection thresholds (zero = inherit center default)

	nextTrigger time.Time    // next scheduled trigger (in-memory)
	triggering  bool         // a trigger round is currently in flight (in-memory, single-flight)
	progress    *progressLog // live per-round stage log for the console (in-memory)
}

// Config holds the center's runtime configuration (--center CLI flags).
type Config struct {
	Port       int               // HTTP listen port
	DataDir    string            // persistence root (businesses.json + per-business results)
	Interval   time.Duration     // default trigger period for new businesses
	Thresholds config.Thresholds // default merged-detection thresholds for new businesses
}

// DefaultConfig returns sensible defaults.
func DefaultConfig() Config {
	return Config{
		Port:       8080,
		DataDir:    "center_data",
		Interval:   10 * time.Minute,
		Thresholds: config.DefaultThresholds(),
	}
}

// persistedState is what survives a restart (including match keys).
type persistedState struct {
	Businesses []*Business `json:"businesses"`
}
