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
	row.NLMTU = MTU
	row.UseAutomaticMetric = false
	row.Metric = 1
	if err := row.Set(); err != nil {
		return fmt.Errorf("the packet size and metric could not be set: %w", err)
	}
	return nil
}

type moved struct {
	LUID      uint64 `json:"luid"`
	Index     int    `json:"index"`
	Alias     string `json:"alias"`
	Metric    int    `json:"metric"`
	Automatic bool   `json:"automatic"`
}

// The record of what TakePriority moved lives in the registry under HKEY_LOCAL_MACHINE, which only administrators can
// write, and not in a file in the user's folder: a file there could be swapped for a link by any program the user runs,
// and the helper, with administrator rights, would then write or replay whatever it points to.
const (
	recordKey   = `SOFTWARE\PrivatePartyNet`
	recordValue = "GameNetworkMovedMetrics"
)

func saveRecord(list []moved) error {
	key, _, err := registry.CreateKey(registry.LOCAL_MACHINE, recordKey, registry.SET_VALUE)
	if err != nil {
		return err
	}
	defer key.Close()
	data, _ := json.Marshal(list)
	return key.SetStringValue(recordValue, string(data))
}

func loadRecord() []moved {
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

func forgetRecord() {
	key, err := registry.OpenKey(registry.LOCAL_MACHINE, recordKey, registry.SET_VALUE)
	if err != nil {
		return
	}
	defer key.Close()
	key.DeleteValue(recordValue)
}

// sane is true for an entry that could have come from TakePriority: an adapter and a metric Windows allows.
func (m moved) sane() bool {
	return m.LUID != 0 && m.Metric >= 0 && m.Metric <= 9999
}

// TakePriority moves every other connected adapter at metric 1 (Radmin VPN, for one) to 2 while the game network is
// on, so the game network's 1 wins. What each one had, including whether Windows chose its metric automatically, is
// recorded FIRST, so GiveBack can put it back even after a crash. Returns what was moved, and what could not be.
func TakePriority(own winipcfg.LUID) ([]moved, []string) {
	var problems []string
	if err := RestoreLeftovers(); err != nil {
		problems = append(problems, err.Error())
	}
	rows, err := winipcfg.GetIPInterfaceTable(windows.AF_INET)
	if err != nil {
		return nil, append(problems, "the other network adapters could not be read: "+err.Error())
	}
	var list []moved
	for _, row := range rows {
		if !row.Connected || row.InterfaceLUID == own || row.Metric > 1 {
			continue
		}
		alias := fmt.Sprintf("adapter %d", row.InterfaceIndex)
		if info, err := row.InterfaceLUID.Interface(); err == nil {
			alias = info.Alias()
		}
		list = append(list, moved{uint64(row.InterfaceLUID), int(row.InterfaceIndex), alias, int(row.Metric),
			row.UseAutomaticMetric})
	}
	if len(list) == 0 {
		return nil, problems
	}
	if err := saveRecord(list); err != nil {
		// without the record nothing could be put back after a crash, so nothing is moved
		return nil, append(problems, "nothing was moved, because what to put back could not be recorded: "+err.Error())
	}
	for _, item := range list {
		if err := setMetric(item.LUID, 2, false); err != nil {
			problems = append(problems, fmt.Sprintf("%s could not be moved behind the game network: %v", item.Alias, err))
		}
	}
	return list, problems
}

func setMetric(luid uint64, metric int, automatic bool) error {
	row, err := winipcfg.LUID(luid).IPInterface(windows.AF_INET)
	if err != nil {
		return err
	}
	row.UseAutomaticMetric = automatic
	if !automatic {
		row.Metric = uint32(metric)
	}
	return row.Set()
}

// GiveBack puts every adapter TakePriority moved back exactly as it was, then forgets the record.
func GiveBack(list []moved) error {
	var problems []error
	for _, item := range list {
		if !item.sane() {
			continue
		}
		if err := setMetric(item.LUID, item.Metric, item.Automatic); err != nil && !errors.Is(err, windows.ERROR_NOT_FOUND) {
			problems = append(problems, fmt.Errorf("%s could not be given its metric back: %w", item.Alias, err))
		}
	}
	forgetRecord()
	return errors.Join(problems...)
}

// RestoreLeftovers gives back what a helper that was killed outright could not, before anything else.
func RestoreLeftovers() error {
	if list := loadRecord(); list != nil {
		return GiveBack(list)
	}
	return nil
}

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
