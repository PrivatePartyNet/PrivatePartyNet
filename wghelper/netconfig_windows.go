package main

// Windows' side of the adapter: its address, its size of packet, its place among the other adapters, and its network
// profile. The same steps the Python helper took before (nethelper.py), with the same commands.

import (
	"encoding/json"
	"fmt"
	"net"
	"net/netip"
	"os/exec"
	"strings"
	"syscall"
	"time"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/registry"
)

// command runs a program with no window and gives what it printed.
var command = func(name string, args ...string) (string, error) {
	cmd := exec.Command(name, args...)
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: windows.CREATE_NO_WINDOW}
	out, err := cmd.CombinedOutput()
	return string(out), err
}

func powershell(script string) (string, error) {
	return command("powershell", "-NoProfile", "-NonInteractive", "-Command", script)
}

// Configure gives the adapter its address, the game network's packet size and metric 1, so games that pick the first
// network card, and broadcasts to 255.255.255.255, go to the game network. No gateway is set: the adapter is never a
// route to the internet.
func Configure(name string, address netip.Addr, network netip.Prefix) error {
	steps := [][]string{
		{"interface", "ipv4", "set", "address", "name=" + name, "source=static", "address=" + address.String(),
			"mask=" + netmask(network), "gateway=none"},
		{"interface", "ipv4", "set", "subinterface", name, fmt.Sprintf("mtu=%d", MTU), "store=active"},
		{"interface", "ipv4", "set", "interface", "interface=" + name, "metric=1"},
	}
	for _, step := range steps {
		if out, err := command("netsh", step...); err != nil {
			return fmt.Errorf("netsh %s: %s", strings.Join(step[:4], " "), strings.TrimSpace(out))
		}
	}
	return nil
}

func netmask(network netip.Prefix) string {
	return net.IP(net.CIDRMask(network.Bits(), 32)).String()
}

type moved struct {
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

// sane is true for an entry that could have come from TakePriority: a real interface number and a metric Windows allows.
func (m moved) sane() bool {
	return m.Index > 0 && m.Index < 1<<24 && m.Metric >= 0 && m.Metric <= 9999
}

// TakePriority moves every other connected adapter at metric 1 (Radmin VPN, for one) to 2 while the game network is
// on, so the game network's 1 wins. What each one had, including whether Windows chose its metric automatically, is
// recorded FIRST, so GiveBack can put it back even after a crash.
func TakePriority(name string) []moved {
	RestoreLeftovers()
	out, err := powershell("Get-NetIPInterface -AddressFamily IPv4 | Where-Object { $_.ConnectionState -eq 'Connected' " +
		"-and $_.InterfaceAlias -ne '" + name + "' -and $_.InterfaceMetric -le 1 } | Select-Object " +
		"InterfaceIndex, InterfaceAlias, InterfaceMetric, @{n='Automatic';e={[string]$_.AutomaticMetric}} " +
		"| ConvertTo-Json -Compress")
	out = strings.TrimSpace(out)
	if err != nil || out == "" {
		return nil
	}
	var found []map[string]any
	if strings.HasPrefix(out, "{") {
		out = "[" + out + "]"
	}
	if json.Unmarshal([]byte(out), &found) != nil {
		return nil
	}
	var list []moved
	for _, item := range found {
		index, _ := item["InterfaceIndex"].(float64)
		metric, _ := item["InterfaceMetric"].(float64)
		alias, _ := item["InterfaceAlias"].(string)
		automatic, _ := item["Automatic"].(string)
		entry := moved{int(index), alias, int(metric), strings.EqualFold(automatic, "enabled")}
		if entry.sane() {
			list = append(list, entry)
		}
	}
	if len(list) == 0 {
		return nil
	}
	if saveRecord(list) != nil {
		return nil // without the record nothing could be put back after a crash, so nothing is moved
	}
	for _, item := range list {
		powershell(fmt.Sprintf("Set-NetIPInterface -InterfaceIndex %d -AddressFamily IPv4 -InterfaceMetric 2", item.Index))
	}
	return list
}

// GiveBack puts every adapter TakePriority moved back exactly as it was, then forgets the record.
func GiveBack(list []moved) {
	for _, item := range list {
		if !item.sane() {
			continue
		}
		if item.Automatic {
			powershell(fmt.Sprintf("Set-NetIPInterface -InterfaceIndex %d -AddressFamily IPv4 -AutomaticMetric Enabled", item.Index))
		} else {
			powershell(fmt.Sprintf("Set-NetIPInterface -InterfaceIndex %d -AddressFamily IPv4 -InterfaceMetric %d",
				item.Index, item.Metric))
		}
	}
	forgetRecord()
}

// RestoreLeftovers gives back what a helper that was killed outright could not, before anything else.
func RestoreLeftovers() {
	if list := loadRecord(); list != nil {
		GiveBack(list)
	}
}

// MakePrivate marks the adapter's network Private, as Radmin VPN does its own. A new adapter with no gateway is an
// unidentified network, which Windows treats as Public, and then the firewall blocks a game that hosts. Windows makes
// the network profile a few seconds after the address is set, so this tries again for a while. The game network works
// without it; games just ask the firewall.
func MakePrivate(name string) bool {
	for try := 0; try < 10; try++ {
		if _, err := powershell("Set-NetConnectionProfile -InterfaceAlias '" + name + "' -NetworkCategory Private"); err == nil {
			return true
		}
		time.Sleep(time.Second)
	}
	return false
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
