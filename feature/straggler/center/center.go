package center

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/Computing-Availability-Tools/CATHelper/feature/straggler/config"
)

// Center is the center-node service: business CRUD + persistence, plus (in
// later stages) daemon matching, health probing, and merged detection.
type Center struct {
	cfg      Config
	mu       sync.Mutex
	detectMu sync.Mutex           // serializes detection (process-wide config globals)
	biz      map[string]*Business // name → business (persisted)
	metrics  *metricsStore        // vllm TTFT/TPOT time series (in-memory)
	logf     func(format string, args ...any)
}

// New creates a Center, loading persisted state from cfg.DataDir.
func New(cfg Config) *Center {
	if cfg.Port <= 0 {
		cfg.Port = 8080
	}
	if cfg.DataDir == "" {
		cfg.DataDir = "center_data"
	}
	if cfg.Interval <= 0 {
		cfg.Interval = 10 * time.Minute
	}
	c := &Center{
		cfg:     cfg,
		biz:     make(map[string]*Business),
		metrics: newMetricsStore(),
		logf:    func(format string, args ...any) { fmt.Fprintf(os.Stderr, "[CENTER] "+format+"\n", args...) },
	}
	// Ensure the data dir exists at startup (created lazily if absent), so a
	// fresh --center-data-dir is ready before the first CRUD / result write.
	_ = os.MkdirAll(c.cfg.DataDir, 0o755)
	c.load()
	return c
}

// Run starts the HTTP server and the heartbeat/schedule loops until ctx is done.
func (c *Center) Run(ctx context.Context) error {
	srv := c.httpServer()
	ln, err := net.Listen("tcp", fmt.Sprintf(":%d", c.cfg.Port))
	if err != nil {
		return fmt.Errorf("HTTP listen :%d: %w", c.cfg.Port, err)
	}
	c.logf("center HTTP server listening on :%d (data=%s)", c.cfg.Port, c.cfg.DataDir)
	srvErr := make(chan error, 1)
	go func() {
		if err := srv.Serve(ln); err != nil && err != http.ErrServerClosed {
			srvErr <- err
		}
	}()

	go c.heartbeatLoop()
	go c.scheduleLoop()
	go c.metricsLoop()

	select {
	case <-ctx.Done():
		_ = srv.Close()
		return nil
	case err := <-srvErr:
		return err
	}
}

// ---------------------------------------------------------------------------
// Persistence
// ---------------------------------------------------------------------------

func (c *Center) statePath() string {
	return filepath.Join(c.cfg.DataDir, "businesses.json")
}

func (c *Center) load() {
	raw, err := os.ReadFile(c.statePath())
	if err != nil {
		return
	}
	var st persistedState
	if json.Unmarshal(raw, &st) != nil {
		return
	}
	for _, b := range st.Businesses {
		if b.Name != "" {
			if b.Thresholds == (config.Thresholds{}) {
				b.Thresholds = c.cfg.Thresholds
			} else {
				b.Thresholds = b.Thresholds.Normalized()
			}
			b.progress = newProgressLog()
			c.biz[b.Name] = b
		}
	}
}

func (c *Center) save() {
	_ = os.MkdirAll(c.cfg.DataDir, 0o755)
	st := persistedState{Businesses: c.listLocked()}
	data, err := json.MarshalIndent(st, "", "  ")
	if err != nil {
		return
	}
	_ = os.WriteFile(c.statePath(), data, 0o644)
}

// ---------------------------------------------------------------------------
// CRUD (callers must hold c.mu for the *Locked variants)
// ---------------------------------------------------------------------------

func (c *Center) listLocked() []*Business {
	out := make([]*Business, 0, len(c.biz))
	for _, b := range c.biz {
		out = append(out, b)
	}
	return out
}

// Business returns a copy-free view of one business (nil if absent).
func (c *Center) Business(name string) *Business {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.biz[name]
}

func (c *Center) daemonIndex(b *Business, ip string, port int) int {
	for i, d := range b.Daemons {
		if d.IP == ip && d.Port == port {
			return i
		}
	}
	return -1
}

// OK reports the center's own health (mirrors a daemon's /healthz shape).
func (c *Center) OK() bool { return true }
