package main

// The WireGuard side of the helper: one wireguard-go device on the wrapped adapter, with a peer for each member.
//
// The app sends the whole picture every time it changes (this PC's key and address, and each member's key and
// address), and the engine works out what to add and what to remove, so a member who was removed or banned is gone
// from WireGuard the moment the app hears of it. Members who stay keep their sessions. Everything the app sends is
// checked here first: the helper runs with administrator rights and takes nothing on trust, so a peer can only ever
// be one address inside this server's /24.

import (
	"encoding/hex"
	"errors"
	"fmt"
	"net/netip"
	"sort"
	"strings"
	"sync"

	"golang.zx2c4.com/wireguard/device"
	"golang.zx2c4.com/wireguard/tun"
)

const (
	KeepaliveSeconds = 25 // keeps each path's router mapping open and shows quickly when a member is gone
	MaxPeers         = 253
)

var gameNetworks = netip.MustParsePrefix("10.0.0.0/8")

// Peer is one member as the app describes them.
type Peer struct {
	Key     string `json:"key"`     // WireGuard public key, hex
	Address string `json:"address"` // their address on the game network
}

// Config is the whole picture the app sends.
type Config struct {
	PrivateKey string `json:"private_key"` // this PC's WireGuard private key for this server, hex
	Address    string `json:"address"`
	Network    string `json:"network"`
	Peers      []Peer `json:"peers"`
}

type checkedPeer struct {
	key     string
	address netip.Addr
}

type checked struct {
	privateKey string
	self       netip.Addr
	network    netip.Prefix
	peers      []checkedPeer
}

func hexKey(value string) (string, error) {
	raw, err := hex.DecodeString(value)
	if err != nil || len(raw) != 32 {
		return "", errors.New("a key is not 32 bytes of hex")
	}
	return hex.EncodeToString(raw), nil
}

// Check makes sure a config can only describe this server's game network.
func Check(c Config) (*checked, error) {
	network, err := netip.ParsePrefix(c.Network)
	if err != nil || !network.Addr().Is4() || network.Bits() != 24 || network != network.Masked() ||
		!gameNetworks.Contains(network.Addr()) {
		return nil, fmt.Errorf("not a game network: %q", c.Network)
	}
	usable := func(a netip.Addr) bool {
		return a.Is4() && network.Contains(a) && a != network.Addr() && a != broadcastOf(network)
	}
	self, err := netip.ParseAddr(c.Address)
	if err != nil || !usable(self) {
		return nil, fmt.Errorf("not an address on the game network: %q", c.Address)
	}
	private, err := hexKey(c.PrivateKey)
	if err != nil {
		return nil, err
	}
	if len(c.Peers) > MaxPeers {
		return nil, errors.New("too many members")
	}
	out := &checked{privateKey: private, self: self, network: network}
	seenKey, seenAddress := map[string]bool{}, map[netip.Addr]bool{self: true}
	for _, p := range c.Peers {
		key, err := hexKey(p.Key)
		if err != nil {
			return nil, err
		}
		address, err := netip.ParseAddr(p.Address)
		if err != nil || !usable(address) || seenAddress[address] || seenKey[key] {
			return nil, fmt.Errorf("a member's address or key is wrong or used twice: %q", p.Address)
		}
		seenKey[key], seenAddress[address] = true, true
		out.peers = append(out.peers, checkedPeer{key, address})
	}
	sort.Slice(out.peers, func(i, j int) bool { return out.peers[i].address.Less(out.peers[j].address) })
	return out, nil
}

type Engine struct {
	dev     *device.Device
	fanout  *Fanout
	bind    *LinkBind
	lock    sync.Mutex
	current *checked
}

// NewEngine starts WireGuard on `adapter` with the first config. `send` carries each datagram to a member.
func NewEngine(adapter tun.Device, c Config, send func(peer byte, data []byte) error, logger *device.Logger) (*Engine, error) {
	first, err := Check(c)
	if err != nil {
		return nil, err
	}
	e := &Engine{bind: NewLinkBind(send)}
	e.fanout = NewFanout(adapter, meshOf(first))
	e.dev = device.NewDevice(e.fanout, e.bind, logger)
	if err := e.dev.IpcSet("private_key=" + first.privateKey + "\n"); err != nil {
		e.dev.Close()
		return nil, err
	}
	if err := e.apply(first); err != nil {
		e.dev.Close()
		return nil, err
	}
	if err := e.dev.Up(); err != nil {
		e.dev.Close()
		return nil, err
	}
	return e, nil
}

func meshOf(c *checked) *Mesh {
	peers := make([]netip.Addr, len(c.peers))
	for i, p := range c.peers {
		peers[i] = p.address
	}
	return &Mesh{Self: c.self, Network: c.network, Peers: peers}
}

// Apply changes the members. This PC's address, network and key stay as they were at the start.
func (e *Engine) Apply(c Config) error {
	next, err := Check(c)
	if err != nil {
		return err
	}
	e.lock.Lock()
	current := e.current
	e.lock.Unlock()
	if next.self != current.self || next.network != current.network || next.privateKey != current.privateKey {
		return errors.New("the address, network or key cannot change while the game network is on")
	}
	return e.apply(next)
}

func (e *Engine) apply(next *checked) error {
	e.lock.Lock()
	defer e.lock.Unlock()
	var b strings.Builder
	wanted := map[string]bool{}
	for _, p := range next.peers {
		wanted[p.key] = true
	}
	if e.current != nil {
		for _, p := range e.current.peers {
			if !wanted[p.key] {
				fmt.Fprintf(&b, "public_key=%s\nremove=true\n", p.key)
			}
		}
	}
	for _, p := range next.peers {
		// Only the lower address of each pair keeps the link alive, and so starts the handshake as soon as it hears of
		// the other. When both start at the same moment, each one's answer cancels the other's attempt, and WireGuard
		// waits five seconds before it tries again.
		keepalive := 0
		if next.self.Less(p.address) {
			keepalive = KeepaliveSeconds
		}
		fmt.Fprintf(&b, "public_key=%s\nreplace_allowed_ips=true\nallowed_ip=%s/32\nendpoint=%s\n"+
			"persistent_keepalive_interval=%d\n", p.key, p.address, PeerEndpoint(p.address.As4()[3]).DstToString(),
			keepalive)
	}
	if b.Len() > 0 {
		if err := e.dev.IpcSet(b.String()); err != nil {
			return err
		}
	}
	e.fanout.SetMesh(meshOf(next))
	e.current = next
	return nil
}

// Deliver hands WireGuard a datagram that arrived from the member at `peer`.
func (e *Engine) Deliver(peer byte, data []byte) bool { return e.bind.Deliver(peer, data) }

// PathUp is called when a way to the member at `peer` starts working. What WireGuard sent before then was dropped, and
// it would only try again after five seconds, so the side that starts handshakes (see apply) starts one now.
func (e *Engine) PathUp(peer byte) {
	e.lock.Lock()
	current := e.current
	e.lock.Unlock()
	for _, p := range current.peers {
		if p.address.As4()[3] != peer || !current.self.Less(p.address) {
			continue
		}
		var key device.NoisePublicKey
		raw, _ := hex.DecodeString(p.key)
		copy(key[:], raw)
		if found := e.dev.LookupPeer(key); found != nil {
			found.SendKeepalive()
		}
	}
}

// Stats is the WireGuard state of each member, as wireguard-go reports it (handshake times, bytes each way).
func (e *Engine) Stats() (string, error) { return e.dev.IpcGet() }

func (e *Engine) Close() { e.dev.Close() }
