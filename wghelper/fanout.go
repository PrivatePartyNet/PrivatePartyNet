package main

// The adapter as WireGuard sees it: the real one (Wintun) wrapped so that only game network traffic passes, and so that
// broadcast and multicast reach every member.
//
// WireGuard sends a packet to the one peer whose allowed address matches its destination, so a broadcast or multicast
// (how LAN games find each other) would go nowhere. Instead, each one Windows sends is wrapped in a UDP packet to each
// member's own address on the fan-out port, which WireGuard carries like any other packet. The member's side takes the
// original out again only when its sender is the same address the wrapper came from. WireGuard has already checked
// that the wrapper came from that member (allowed addresses are each member's own /32), so nobody can send a
// broadcast in someone else's name.

import (
	"encoding/binary"
	"net/netip"
	"os"
	"sync"
	"sync/atomic"

	"golang.zx2c4.com/wireguard/tun"
)

const (
	FanoutPort  = 8767 // never reaches Windows: the wrapper is made and taken apart inside the helper
	ipHeader    = 20
	udpHeader   = 8
	maxPending  = 4096 // wrapped packets waiting for WireGuard to read them; more are dropped
	fanoutExtra = ipHeader + udpHeader + len(fanoutMagic)
)

const fanoutMagic = "PPNB"

// Mesh is what the filter needs to know about the network: its own address, the /24, and the members to fan out to.
type Mesh struct {
	Self    netip.Addr
	Network netip.Prefix
	Peers   []netip.Addr
}

// Fanout wraps a tun.Device. Read gives WireGuard only what belongs on the game network, with broadcast and multicast
// turned into one wrapped packet per member; Write hands Windows only what belongs on it, with wrappers taken apart.
type Fanout struct {
	inner   tun.Device
	mesh    atomic.Pointer[Mesh]
	lock    sync.Mutex
	pending [][]byte
	Dropped atomic.Uint64
}

func NewFanout(inner tun.Device, mesh *Mesh) *Fanout {
	f := &Fanout{inner: inner}
	f.mesh.Store(mesh)
	return f
}

// SetMesh changes the members broadcasts go to. Safe while packets flow.
func (f *Fanout) SetMesh(mesh *Mesh) { f.mesh.Store(mesh) }

func (f *Fanout) File() *os.File           { return f.inner.File() }
func (f *Fanout) MTU() (int, error)        { return f.inner.MTU() }
func (f *Fanout) Name() (string, error)    { return f.inner.Name() }
func (f *Fanout) Events() <-chan tun.Event { return f.inner.Events() }
func (f *Fanout) Close() error             { return f.inner.Close() }
func (f *Fanout) BatchSize() int           { return f.inner.BatchSize() }

// IsGroup is true for a packet to everyone: the network's broadcast address, 255.255.255.255, or multicast.
func IsGroup(destination netip.Addr, network netip.Prefix) bool {
	return destination == broadcastOf(network) || destination == netip.AddrFrom4([4]byte{255, 255, 255, 255}) ||
		destination.IsMulticast()
}

func broadcastOf(network netip.Prefix) netip.Addr {
	a := network.Masked().Addr().As4()
	host := uint32(1)<<(32-network.Bits()) - 1
	value := binary.BigEndian.Uint32(a[:]) | host
	binary.BigEndian.PutUint32(a[:], value)
	return netip.AddrFrom4(a)
}

// Parse gives (source, destination) of a well-formed IPv4 packet, and ok false for anything else.
func Parse(packet []byte) (source, destination netip.Addr, ok bool) {
	if len(packet) < ipHeader || packet[0]>>4 != 4 {
		return
	}
	header := int(packet[0]&0x0F) * 4
	total := int(binary.BigEndian.Uint16(packet[2:4]))
	if header < ipHeader || len(packet) < header || total < header || total > len(packet) {
		return
	}
	return netip.AddrFrom4([4]byte(packet[12:16])), netip.AddrFrom4([4]byte(packet[16:20])), true
}

// Read fills bufs with packets for WireGuard to send.
func (f *Fanout) Read(bufs [][]byte, sizes []int, offset int) (int, error) {
	for {
		if n := f.takePending(bufs, sizes, offset); n > 0 {
			return n, nil
		}
		count, err := f.inner.Read(bufs, sizes, offset)
		kept := 0
		mesh := f.mesh.Load()
		for i := 0; i < count; i++ {
			packet := bufs[i][offset : offset+sizes[i]]
			source, destination, ok := Parse(packet)
			switch {
			case !ok || source != mesh.Self:
				f.Dropped.Add(1) // IPv6, or something Windows sent from another address
			case IsGroup(destination, mesh.Network):
				f.queueWrapped(packet, mesh)
			case mesh.Network.Contains(destination) && destination != mesh.Self:
				if kept != i {
					copy(bufs[kept][offset:], packet)
				}
				sizes[kept] = sizes[i]
				kept++
			default:
				f.Dropped.Add(1) // the internet, a home network: never carried
			}
		}
		if kept > 0 || err != nil {
			return kept, err
		}
	}
}

func (f *Fanout) queueWrapped(packet []byte, mesh *Mesh) {
	f.lock.Lock()
	defer f.lock.Unlock()
	for _, peer := range mesh.Peers {
		if len(f.pending) >= maxPending {
			f.Dropped.Add(1)
			continue
		}
		f.pending = append(f.pending, Wrap(packet, mesh.Self, peer))
	}
}

func (f *Fanout) takePending(bufs [][]byte, sizes []int, offset int) int {
	f.lock.Lock()
	defer f.lock.Unlock()
	n := 0
	for n < len(bufs) && len(f.pending) > 0 {
		packet := f.pending[0]
		f.pending = f.pending[1:]
		if len(bufs[n])-offset < len(packet) {
			f.Dropped.Add(1)
			continue
		}
		sizes[n] = copy(bufs[n][offset:], packet)
		n++
	}
	if len(f.pending) == 0 {
		f.pending = nil // let the backing array go
	}
	return n
}

// Write hands Windows the packets WireGuard received and checked.
func (f *Fanout) Write(bufs [][]byte, offset int) (int, error) {
	mesh := f.mesh.Load()
	out := make([][]byte, 0, len(bufs))
	for _, buf := range bufs {
		packet := buf[offset:]
		source, destination, ok := Parse(packet)
		if !ok {
			f.Dropped.Add(1)
			continue
		}
		if inner, wrapped := Unwrap(packet, source, mesh); wrapped {
			if inner != nil {
				out = append(out, inner)
			} else {
				f.Dropped.Add(1)
			}
			continue
		}
		if destination == mesh.Self && mesh.Network.Contains(source) {
			out = append(out, packet)
		} else {
			f.Dropped.Add(1)
		}
	}
	if len(out) > 0 {
		if _, err := f.inner.Write(out, 0); err != nil {
			return 0, err
		}
	}
	return len(bufs), nil
}

// fragmented is true for any piece of a datagram that was cut up: more fragments follow, or this is not the first.
func fragmented(packet []byte) bool { return binary.BigEndian.Uint16(packet[6:8])&0x3FFF != 0 }

// Wrap puts a packet inside a UDP packet from `self` to `peer` on the fan-out port.
func Wrap(packet []byte, self, peer netip.Addr) []byte {
	total := fanoutExtra + len(packet)
	out := make([]byte, total)
	out[0] = 0x45
	binary.BigEndian.PutUint16(out[2:4], uint16(total))
	out[8] = 64 // time to live
	out[9] = 17 // UDP
	s, d := self.As4(), peer.As4()
	copy(out[12:16], s[:])
	copy(out[16:20], d[:])
	binary.BigEndian.PutUint16(out[10:12], checksum(out[:ipHeader]))
	udp := out[ipHeader:]
	binary.BigEndian.PutUint16(udp[0:2], FanoutPort)
	binary.BigEndian.PutUint16(udp[2:4], FanoutPort)
	binary.BigEndian.PutUint16(udp[4:6], uint16(total-ipHeader))
	// a UDP checksum of zero means none, which IPv4 allows; WireGuard already protects the packet
	copy(udp[udpHeader:], fanoutMagic)
	copy(udp[udpHeader+len(fanoutMagic):], packet)
	return out
}

// Unwrap takes a fan-out wrapper apart. wrapped is false when the packet is not a wrapper (it is then an ordinary
// packet). For a wrapper, inner is the original packet, or nil when it must be dropped: its sender is not the address
// the wrapper came from, it is not to everyone, or the wrapper is not for this PC.
func Unwrap(packet []byte, outerSource netip.Addr, mesh *Mesh) (inner []byte, wrapped bool) {
	header := int(packet[0]&0x0F) * 4
	total := int(binary.BigEndian.Uint16(packet[2:4]))
	if packet[9] != 17 || total < header+udpHeader+len(fanoutMagic) || fragmented(packet) {
		return nil, false // the helper never fragments a wrapper, so a fragment is never one
	}
	udp := packet[header:total]
	if binary.BigEndian.Uint16(udp[0:2]) != FanoutPort || binary.BigEndian.Uint16(udp[2:4]) != FanoutPort ||
		string(udp[udpHeader:udpHeader+len(fanoutMagic)]) != fanoutMagic {
		return nil, false
	}
	_, outerDestination, _ := Parse(packet)
	inner = udp[udpHeader+len(fanoutMagic):]
	source, destination, ok := Parse(inner)
	if !ok || outerDestination != mesh.Self || source != outerSource || !mesh.Network.Contains(source) ||
		!IsGroup(destination, mesh.Network) {
		return nil, true
	}
	return inner[:binary.BigEndian.Uint16(inner[2:4])], true
}

func checksum(data []byte) uint16 {
	var sum uint32
	for i := 0; i+1 < len(data); i += 2 {
		sum += uint32(data[i])<<8 | uint32(data[i+1])
	}
	if len(data)%2 == 1 {
		sum += uint32(data[len(data)-1]) << 8
	}
	for sum>>16 != 0 {
		sum = sum&0xFFFF + sum>>16
	}
	return ^uint16(sum)
}
