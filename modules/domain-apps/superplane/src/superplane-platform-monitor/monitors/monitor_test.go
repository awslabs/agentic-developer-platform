package monitors

import (
	"context"
	"errors"
	"testing"
	"time"

	"go.uber.org/zap"
)

// fakeMonitor tracks how many times it was called.
type fakeMonitor struct {
	name      string
	callCount int
	err       error
}

func (f *fakeMonitor) Name() string { return f.name }

func (f *fakeMonitor) Check(_ context.Context) error {
	f.callCount++
	return f.err
}

func TestRunner_RunsMonitorsOnce(t *testing.T) {
	mon1 := &fakeMonitor{name: "rec1"}
	mon2 := &fakeMonitor{name: "rec2"}

	runner := NewRunner(1*time.Hour, zap.NewNop(), mon1, mon2)

	// Use a context that cancels immediately after the first tick.
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()

	_ = runner.Run(ctx)

	// Both monitors should have been called at least once (the immediate run).
	if mon1.callCount < 1 {
		t.Errorf("mon1 should have been called at least once, got %d", mon1.callCount)
	}
	if mon2.callCount < 1 {
		t.Errorf("mon2 should have been called at least once, got %d", mon2.callCount)
	}
}

func TestWorstStatus_Table(t *testing.T) {
	tests := []struct {
		name     string
		statuses []string
		want     string
	}{
		{"all healthy", []string{"Healthy", "Healthy"}, "Healthy"},
		{"one degraded", []string{"Healthy", "Degraded"}, "Degraded"},
		{"unreachable wins", []string{"Degraded", "Unreachable"}, "Unreachable"},
		{"unknown beats healthy", []string{"Healthy", "Unknown"}, "Unknown"},
		{"empty", []string{}, "Healthy"},
		// #5056: a confirmed Unreachable must outrank an unexplained Unknown.
		// The pre-#5056 severity map ranked Unknown above Unreachable, so this
		// case aggregated to "Unknown" and downgraded a proven outage to an open
		// question.
		{"unreachable outranks unknown", []string{"Unknown", "Unreachable"}, "Unreachable"},
		// NotChecked is worse than Healthy — it must not be mistaken for a pass...
		{"not checked beats healthy", []string{"Healthy", "NotChecked"}, "NotChecked"},
		// ...but it must not mask a real finding from a dimension that did run.
		{"degraded outranks not checked", []string{"NotChecked", "Degraded"}, "Degraded"},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := WorstStatus(tt.statuses...)
			if got != tt.want {
				t.Errorf("WorstStatus(%v) = %s, want %s", tt.statuses, got, tt.want)
			}
		})
	}
}

func TestRealClock_ReturnsUTC(t *testing.T) {
	c := RealClock{}
	now := c.Now()
	if now.Location() != time.UTC {
		t.Errorf("RealClock.Now() should return UTC, got %s", now.Location())
	}
}

// #5056: replaces TestNoopEKSProber, which asserted the defect — that the
// default prober returns nil, i.e. reports success for a probe it never ran.
func TestUnconfiguredEKSProber_ReportsProbeNotPerformed(t *testing.T) {
	p := UnconfiguredEKSProber{}
	err := p.ProbeEKS(context.Background(), nil)
	if err == nil {
		t.Fatal("UnconfiguredEKSProber must not return nil: a nil error is indistinguishable from a successful probe")
	}
	if !errors.Is(err, ErrProbeNotPerformed) {
		t.Errorf("error should be ErrProbeNotPerformed so callers can tell it apart from a real probe failure, got %v", err)
	}
}
