package main

// WireGuard's network connection, with no socket of its own. Each datagram WireGuard sends is handed to the app over
// the helper's loopback link, tagged with the member it is for, and the app sends it along that member's path (direct,
// hole punched, through a friend's PC or through the relay). Datagrams the app receives on a member's path come back
// the same way. The member is named by the last byte of their game network address, which is unique on a /24.

import (
	"errors"
	"fmt"
	"net"
	"net/netip"
	"strconv"
	"sync"

	"golang.zx2c4.com/wireguard/conn"
)

const inboxSize = 1024 // datagrams from the app waiting for WireGuard; more are dropped

// PeerEndpoint is a member, by the last byte of their address.
type PeerEndpoint byte

func (e PeerEndpoint) ClearSrc()           {}
func (e PeerEndpoint) SrcToString() string { return "" }
func (e PeerEndpoint) DstToString() string { return "peer:" + strconv.Itoa(int(e)) }
func (e PeerEndpoint) DstToBytes() []byte  { return []byte{byte(e)} }
func (e PeerEndpoint) DstIP() netip.Addr   { return netip.Addr{} }
func (e PeerEndpoint) SrcIP() netip.Addr   { return netip.Addr{} }

type datagram struct {
	peer byte
	data []byte
}

// LinkBind is the conn.Bind. `send` is called with each outgoing datagram and must not block for long.
type LinkBind struct {
	send   func(peer byte, data []byte) error
	inbox  chan datagram
	lock   sync.Mutex
	closed chan struct{}
}

func NewLinkBind(send func(peer byte, data []byte) error) *LinkBind {
	return &LinkBind{send: send, inbox: make(chan datagram, inboxSize)}
}

// Deliver hands WireGuard one datagram that arrived from a member. Dropped when WireGuard is behind or closed.
func (b *LinkBind) Deliver(peer byte, data []byte) bool {
	select {
	case b.inbox <- datagram{peer, data}:
		return true
	default:
		return false
	}
}

func (b *LinkBind) Open(port uint16) ([]conn.ReceiveFunc, uint16, error) {
	b.lock.Lock()
	defer b.lock.Unlock()
	if b.closed != nil {
		return nil, 0, conn.ErrBindAlreadyOpen
	}
	closed := make(chan struct{})
	b.closed = closed
	receive := func(packets [][]byte, sizes []int, eps []conn.Endpoint) (int, error) {
		select {
		case <-closed:
			return 0, net.ErrClosed
		case d := <-b.inbox:
			sizes[0] = copy(packets[0], d.data)
			eps[0] = PeerEndpoint(d.peer)
			return 1, nil
		}
	}
	return []conn.ReceiveFunc{receive}, port, nil
}

func (b *LinkBind) Close() error {
	b.lock.Lock()
	defer b.lock.Unlock()
	if b.closed != nil {
		close(b.closed)
		b.closed = nil
	}
	return nil
}

func (b *LinkBind) SetMark(uint32) error { return nil }
func (b *LinkBind) BatchSize() int       { return 1 }

func (b *LinkBind) Send(bufs [][]byte, ep conn.Endpoint) error {
	peer, ok := ep.(PeerEndpoint)
	if !ok {
		return conn.ErrWrongEndpointType
	}
	for _, buf := range bufs {
		if err := b.send(byte(peer), buf); err != nil {
			return err
		}
	}
	return nil
}

// ParseEndpoint reads "peer:N", the form DstToString writes.
func (b *LinkBind) ParseEndpoint(s string) (conn.Endpoint, error) {
	var n int
	if _, err := fmt.Sscanf(s, "peer:%d", &n); err != nil || n < 1 || n > 254 {
		return nil, errors.New("not a member endpoint: " + s)
	}
	return PeerEndpoint(n), nil
}
