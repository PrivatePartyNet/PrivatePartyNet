package main

// An adapter in memory, for tests that run the real helper without administrator rights: what "Windows" sends arrives
// from the app as 'T' frames, and what is delivered to "Windows" goes back to the app as 'T' frames.

import (
	"os"
	"sync"

	"golang.zx2c4.com/wireguard/tun"
)

type MemoryTun struct {
	link     *Link
	outgoing chan []byte
	events   chan tun.Event
	closed   chan struct{}
	once     sync.Once
}

func NewMemoryTun(link *Link) *MemoryTun {
	t := &MemoryTun{link: link, outgoing: make(chan []byte, 1024), events: make(chan tun.Event, 1),
		closed: make(chan struct{})}
	t.events <- tun.EventUp
	return t
}

// Put is a packet "Windows" sent on the adapter.
func (t *MemoryTun) Put(packet []byte) {
	select {
	case t.outgoing <- packet:
	default:
	}
}

func (t *MemoryTun) Read(bufs [][]byte, sizes []int, offset int) (int, error) {
	select {
	case <-t.closed:
		return 0, os.ErrClosed
	case packet := <-t.outgoing:
		sizes[0] = copy(bufs[0][offset:], packet)
		return 1, nil
	}
}

func (t *MemoryTun) Write(bufs [][]byte, offset int) (int, error) {
	for _, buf := range bufs {
		t.link.Send('T', buf[offset:])
	}
	return len(bufs), nil
}

func (t *MemoryTun) File() *os.File           { return nil }
func (t *MemoryTun) MTU() (int, error)        { return MTU, nil }
func (t *MemoryTun) Name() (string, error)    { return AdapterName, nil }
func (t *MemoryTun) Events() <-chan tun.Event { return t.events }
func (t *MemoryTun) BatchSize() int           { return 1 }

func (t *MemoryTun) Close() error {
	t.once.Do(func() {
		close(t.closed)
		close(t.events)
	})
	return nil
}
