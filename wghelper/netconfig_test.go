package main

import (
	"testing"
	"unsafe"

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

func TestTheGameNetworksConnectionIsFoundByItsAdapterAndTheRestLetGo(t *testing.T) {
	ours := windows.GUID{Data1: 0xA, Data4: [8]byte{1, 2, 3}}
	handles := []byte{1, 2, 3}
	ids := []windows.GUID{{Data1: 0xB}, {}, ours}
	var dropped []unsafe.Pointer
	walk := func(list []windows.GUID) func() (windows.GUID, unsafe.Pointer, bool) {
		i := 0
		return func() (windows.GUID, unsafe.Pointer, bool) {
			if i == len(list) {
				return windows.GUID{}, nil, false
			}
			i++
			return list[i-1], unsafe.Pointer(&handles[i-1]), true
		}
	}
	found := pickConnection(ours, walk(ids), func(p unsafe.Pointer) { dropped = append(dropped, p) })
	if found != unsafe.Pointer(&handles[2]) || len(dropped) != 2 {
		t.Fatalf("found %v, let go of %d", found, len(dropped))
	}
	dropped = nil
	if pickConnection(ours, walk(ids[:2]), func(p unsafe.Pointer) { dropped = append(dropped, p) }) != nil || len(dropped) != 2 {
		t.Fatal("a network that is not there yet was found, or a handle was kept")
	}
	if pickConnection(windows.GUID{}, walk(ids), func(unsafe.Pointer) {}) != nil {
		t.Fatal("an unknown adapter (all zero) matched a connection whose id could not be read")
	}
}
