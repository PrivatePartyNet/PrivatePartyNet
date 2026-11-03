package main

// Windows' own services that party members cannot reach on this PC unless its owner opens them.
//
// The game network is marked Private, so that a game hosting on this PC is let through the firewall. Windows opens
// file sharing, remote desktop and the like to Private networks too, and a game network is everyone in the party. So
// members' packets to these services are dropped here, before Windows sees them, unless the owner of this PC has
// turned that group on in the control panel. No LAN game uses these ports: games find each other with their own
// broadcasts and multicasts, and SSDP (1900) is how a PC talks to its own router, which the game network has none of.
//
// A datagram cut into fragments is judged by its first fragment, which carries the port; without it Windows never puts
// the datagram together.

import (
	"encoding/binary"
	"fmt"
	"sort"
)

const (
	tcp = 6
	udp = 17
)

type portRule struct {
	protocol  byte
	low, high uint16
}

// ServiceGroups are the groups the app can open, by name, each with its ports. Keep the names in step with
// SERVICE_GROUPS in privatepartynet/gamenet.py.
var ServiceGroups = map[string][]portRule{
	"sharing": { // Windows file and printer sharing, and the NetBIOS names and datagrams that go with it
		{tcp, 445, 445}, {tcp, 137, 139}, {udp, 137, 139},
	},
	"remote_desktop": {{tcp, 3389, 3389}, {udp, 3389, 3389}},
	"remote_management": { // WinRM, and the RPC endpoint mapper that remote administration tools start from
		{tcp, 5985, 5986}, {tcp, 135, 135},
	},
	"discovery": { // network discovery: WS-Discovery, function discovery, SSDP and UPnP devices, and LLMNR names
		{tcp, 5357, 5358}, {udp, 3702, 3702}, {udp, 1900, 1900}, {tcp, 2869, 2869}, {udp, 5355, 5355},
		{tcp, 5355, 5355},
	},
}

// closedRules are the ports of every group not in `open`.
func closedRules(open []string) []portRule {
	opened := map[string]bool{}
	for _, name := range open {
		opened[name] = true
	}
	names := make([]string, 0, len(ServiceGroups))
	for name := range ServiceGroups {
		names = append(names, name)
	}
	sort.Strings(names)
	var rules []portRule
	for _, name := range names {
		if !opened[name] {
			rules = append(rules, ServiceGroups[name]...)
		}
	}
	return rules
}

func checkOpen(open []string) error {
	for _, name := range open {
		if _, ok := ServiceGroups[name]; !ok {
			return fmt.Errorf("not a group of Windows services: %q", name)
		}
	}
	return nil
}

// ToClosedService is true for a TCP or UDP packet to one of the closed ports.
func ToClosedService(packet []byte, closed []portRule) bool {
	header := int(packet[0]&0x0F) * 4
	protocol := packet[9]
	if (protocol != tcp && protocol != udp) || fragmentOffset(packet) != 0 || len(packet) < header+4 {
		return false
	}
	port := binary.BigEndian.Uint16(packet[header+2 : header+4])
	for _, rule := range closed {
		if rule.protocol == protocol && port >= rule.low && port <= rule.high {
			return true
		}
	}
	return false
}

func fragmentOffset(packet []byte) int { return int(binary.BigEndian.Uint16(packet[6:8]) & 0x1FFF) }
