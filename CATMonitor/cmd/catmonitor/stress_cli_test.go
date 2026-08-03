package main

import (
	"reflect"
	"testing"

	"github.com/Computing-Availability-Tools/CATMonitor/features/stress"
)

func TestParseStressRunArgs(t *testing.T) {
	path, names, output, err := parseStressRunArgs([]string{
		"--bench", "stream,hpcg", "-c", "test.yaml", "-o", "table",
	})
	if err != nil {
		t.Fatal(err)
	}
	if path != "test.yaml" || output != "table" || !reflect.DeepEqual(names, []string{"stream", "hpcg"}) {
		t.Fatalf("path=%q names=%v output=%q", path, names, output)
	}
}

func TestParseStressRunArgsRejectsInvalidInput(t *testing.T) {
	for _, args := range [][]string{
		{"--bench", "stream,", "-o", "json"},
		{"--bench", "stream", "-o", "yaml"},
		{"unexpected"},
	} {
		if _, _, _, err := parseStressRunArgs(args); err == nil {
			t.Fatalf("args %v unexpectedly accepted", args)
		}
	}
}

func TestStressStatusAndValueFormatting(t *testing.T) {
	if got := stressStatusLabel(stress.StatusHealthy); got != "OK" {
		t.Fatalf("healthy label=%q", got)
	}
	if got := stressStatusLabel(stress.StatusTimeLimitReached); got != "OK (time limit)" {
		t.Fatalf("time-limit label=%q", got)
	}
	if got := formatStressValue(12); got != "12" {
		t.Fatalf("integer value=%q", got)
	}
	if got := formatStressValue(12.345); got != "12.35" {
		t.Fatalf("decimal value=%q", got)
	}
}
