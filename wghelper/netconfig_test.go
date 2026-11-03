package main

import (
	"testing"

	"golang.org/x/sys/windows"
	"golang.zx2c4.com/wireguard/windows/tunnel/winipcfg"
)

// These read this PC's own adapters without changing anything, so they need no administrator rights. Changing a
// profile or a metric is checked on a real adapter by tools/spikes/real_gamenet_check.py.

func TestTheOtherAdaptersAndTheirMetricsAreReadWithoutWMI(t *testing.T) {
	rows, err := winipcfg.GetIPInterfaceTable(windows.AF_INET)
	if err != nil || len(rows) == 0 {
		t.Fatalf("no adapters: %v", err)
	}
	loopback := false
	for _, row := range rows {
		if info, err := row.InterfaceLUID.Interface(); err == nil && info.Type == winipcfg.IfTypeSoftwareLoopback {
			loopback = row.Metric > 0
		}
	}
	if !loopback {
		t.Fatal("the loopback adapter and its metric were not seen")
	}
}

func TestANetworksProfileIsReadThroughTheNetworkListManager(t *testing.T) {
	rows, _ := winipcfg.GetIPInterfaceTable(windows.AF_INET)
	read := 0
	for _, row := range rows {
		if !row.Connected {
			continue
		}
		guid, err := row.InterfaceLUID.GUID()
		if err != nil {
			continue
		}
		category, err := networkCategory(*guid, -1)
		if err != nil {
			t.Logf("%v: %v", row.InterfaceIndex, err)
			continue // loopback and the like have no network of their own
		}
		if category < categoryPublic || category > 2 {
			t.Fatalf("a profile of %d", category)
		}
		t.Logf("adapter %d: profile %d", row.InterfaceIndex, category)
		read++
	}
	if read == 0 {
		t.Skip("this PC has no connected network with a profile")
	}
}

func TestOnlyARecordThatCouldHaveComeFromTakePriorityIsReplayed(t *testing.T) {
	for _, entry := range []moved{{LUID: 0, Metric: 1}, {LUID: 5, Metric: -1}, {LUID: 5, Metric: 10000}} {
		if entry.sane() {
			t.Errorf("%+v was taken as sane", entry)
		}
	}
	if !(moved{LUID: 5, Metric: 1}).sane() {
		t.Error("a real entry was refused")
	}
}
