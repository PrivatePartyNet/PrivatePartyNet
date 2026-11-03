package main

// Windows' side of the adapter: its address, its size of packet, its place among the other adapters, and its network
// profile. Everything goes through Windows' own interfaces (the IP Helper API, as WireGuard for Windows uses, and the
// Network List Manager), not through PowerShell: on some PCs the WMI classes behind Get-NetIPInterface and
// Set-NetConnectionProfile are missing ("Invalid class"), and the old steps then did nothing without a word.

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/netip"
	"time"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/registry"
	"golang.zx2c4.com/wireguard/windows/tunnel/winipcfg"
)

// Configure gives the adapter its address, the game network's packet size and metric 1, so games that pick the first
// network card, and broadcasts to 255.255.255.255, go to the game network. No gateway is set: the adapter is never a
// route to the internet.
func Configure(luid winipcfg.LUID, address netip.Addr, network netip.Prefix) error {
	if err := luid.SetIPAddressesForFamily(windows.AF_INET, []netip.Prefix{netip.PrefixFrom(address, network.Bits())}); err != nil {
		return fmt.Errorf("the address could not be set: %w", err)
	}
	row, err := luid.IPInterface(windows.AF_INET)
	if err != nil {
		return fmt.Errorf("the adapter could not be read: %w", err)
	}
	row.NLMTU = uint32(MTU)
	row.UseAutomaticMetric = false
	row.Metric = 1
	if err := row.Set(); err != nil {
		return fmt.Errorf("the packet size and metric could not be set: %w", err)
	}
	return nil
}

// The record of what takePriority moved lives in the registry under HKEY_LOCAL_MACHINE, which only administrators can
// write, and not in a file in the user's folder: a file there could be swapped for a link by any program the user runs,
// and the helper, with administrator rights, would then write or replay whatever it points to.
const (
	recordKey   = `SOFTWARE\PrivatePartyNet`
	recordValue = "GameNetworkMovedMetrics"
)

type registryRecord struct{}

func (registryRecord) save(list []moved) error {
	key, _, err := registry.CreateKey(registry.LOCAL_MACHINE, recordKey, registry.SET_VALUE)
	if err != nil {
		return err
	}
	defer key.Close()
	data, _ := json.Marshal(list)
	return key.SetStringValue(recordValue, string(data))
}

func (registryRecord) load() []moved {
	key, err := registry.OpenKey(registry.LOCAL_MACHINE, recordKey, registry.QUERY_VALUE)
	if err != nil {
		return nil
	}
	defer key.Close()
	data, _, err := key.GetStringValue(recordValue)
	if err != nil {
		return nil
	}
	var list []moved
	json.Unmarshal([]byte(data), &list)
	return list
}

func (registryRecord) forget() {
	key, err := registry.OpenKey(registry.LOCAL_MACHINE, recordKey, registry.SET_VALUE)
	if err != nil {
		return
	}
	defer key.Close()
	key.DeleteValue(recordValue)
}

// ipHelper is Windows' adapter table through the IP Helper API.
type ipHelper struct{}

func (ipHelper) list() ([]adapterState, error) {
	rows, err := winipcfg.GetIPInterfaceTable(windows.AF_INET)
	if err != nil {
		return nil, err
	}
	out := make([]adapterState, 0, len(rows))
	for _, row := range rows {
		alias := fmt.Sprintf("adapter %d", row.InterfaceIndex)
		if info, err := row.InterfaceLUID.Interface(); err == nil {
			alias = info.Alias()
		}
		out = append(out, adapterState{uint64(row.InterfaceLUID), int(row.InterfaceIndex), alias, int(row.Metric),
			row.UseAutomaticMetric, row.Connected})
	}
	return out, nil
}

func (ipHelper) setMetric(luid uint64, metric int, automatic bool) error {
	row, err := winipcfg.LUID(luid).IPInterface(windows.AF_INET)
	if errors.Is(err, windows.ERROR_NOT_FOUND) || errors.Is(err, windows.ERROR_FILE_NOT_FOUND) {
		return errGone
	}
	if err != nil {
		return err
	}
	row.UseAutomaticMetric = automatic
	if !automatic {
		row.Metric = uint32(metric)
	}
	return row.Set()
}

// TakePriority, GiveBack and RestoreLeftovers are priority.go's, on this PC's adapters and the registry record.
func TakePriority(own winipcfg.LUID) ([]moved, []string) {
	return takePriority(uint64(own), ipHelper{}, registryRecord{})
}

func GiveBack(list []moved) error { return giveBack(list, ipHelper{}, registryRecord{}) }

func RestoreLeftovers() error { return restoreLeftovers(ipHelper{}, registryRecord{}) }

// MakePrivate marks the adapter's network Private, as Radmin VPN does its own. A new adapter with no gateway is an
// unidentified network, which Windows treats as Public, and then the firewall blocks a game that hosts. Windows makes
// the network a few seconds after the address is set, so this tries again for a while. Returns nil once Windows says
// the network is Private.
func MakePrivate(luid winipcfg.LUID) error {
	guid, err := luid.GUID()
	if err != nil {
		return err
	}
	last := errors.New("Windows did not make the network")
	for try := 0; try < 20; try++ {
		category, err := networkCategory(*guid, categoryPrivate)
		if err == nil && category == categoryPrivate {
			return nil
		}
		if err != nil {
			last = err
		} else {
			last = errors.New("Windows kept the network Public")
		}
		time.Sleep(time.Second)
	}
	return last
}

// ParentAlive is true while the process `pid` is running.
func ParentAlive(pid int) bool {
	if pid == 0 {
		return true
	}
	handle, err := windows.OpenProcess(windows.SYNCHRONIZE, false, uint32(pid))
	if err != nil {
		return false
	}
	defer windows.CloseHandle(handle)
	event, _ := windows.WaitForSingleObject(handle, 0)
	return event == uint32(windows.WAIT_TIMEOUT)
}
