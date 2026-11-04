package main

// The loopback link between the helper and the app. Frames both ways: two bytes of length (big-endian, counting what
// follows), one byte of kind, then the body.
//
//	app to helper:  'C' config (JSON, see Config)    'D' member byte + datagram that arrived from that member
//	                'U' member byte: a way to that member started working
//	                'T' a packet Windows sent (only with --memory-adapter, for tests)
//	helper to app:  'D' member byte + datagram to send to that member
//	                'S' status (JSON {"state", "message"})    'L' a line for the app's log
//	                'T' a packet for Windows (only with --memory-adapter)

import (
	"bufio"
	"encoding/binary"
	"encoding/json"
	"errors"
	"io"
	"net"
	"sync"
	"time"
)

const (
	maxFrame   = 65535
	outboxSize = 2048 // frames waiting for the app; more are dropped (WireGuard copes with lost datagrams)
)

type Frame struct {
	Kind byte
	Body []byte
}

// ReadFrame reads one frame.
func ReadFrame(r io.Reader) (Frame, error) {
	var header [2]byte
	if _, err := io.ReadFull(r, header[:]); err != nil {
		return Frame{}, err
	}
	length := int(binary.BigEndian.Uint16(header[:]))
	if length == 0 {
		return Frame{}, errors.New("an empty frame")
	}
	data := make([]byte, length)
	if _, err := io.ReadFull(r, data); err != nil {
		return Frame{}, err
	}
	return Frame{data[0], data[1:]}, nil
}

// Link writes frames to the app from any goroutine, without waiting on the app.
type Link struct {
	conn   net.Conn
	reader *bufio.Reader
	outbox chan []byte
	done   chan struct{}
	once   sync.Once
}

func NewLink(conn net.Conn) *Link {
	l := &Link{conn: conn, reader: bufio.NewReaderSize(conn, 1<<16), outbox: make(chan []byte, outboxSize),
		done: make(chan struct{})}
	go l.write()
	return l
}

func encode(kind byte, parts ...[]byte) []byte {
	length := 1
	for _, p := range parts {
		length += len(p)
	}
	if length > maxFrame {
		return nil
	}
	out := make([]byte, 2, 2+length)
	binary.BigEndian.PutUint16(out, uint16(length))
	out = append(out, kind)
	for _, p := range parts {
		out = append(out, p...)
	}
	return out
}

// Send queues a frame. False when it was dropped (the app is behind, or the link is closed).
func (l *Link) Send(kind byte, parts ...[]byte) bool {
	frame := encode(kind, parts...)
	if frame == nil {
		return false
	}
	select {
	case <-l.done:
		return false
	default:
	}
	select {
	case l.outbox <- frame:
		return true
	default:
		return false
	}
}

// SendJSON queues a frame with a JSON body.
func (l *Link) SendJSON(kind byte, value any) bool {
	body, err := json.Marshal(value)
	return err == nil && l.Send(kind, body)
}

func (l *Link) write() {
	w := bufio.NewWriterSize(l.conn, 1<<16)
	for {
		select {
		case <-l.done:
			return
		case frame := <-l.outbox:
			if _, err := w.Write(frame); err != nil {
				l.Close()
				return
			}
			if len(l.outbox) == 0 { // flush when nothing else is waiting, so a burst goes out in one write
				if err := w.Flush(); err != nil {
					l.Close()
					return
				}
			}
		}
	}
}

// Read reads the next frame from the app.
func (l *Link) Read() (Frame, error) { return ReadFrame(l.reader) }

// Drain waits until the frames queued so far are written, or `limit` has passed.
func (l *Link) Drain(limit time.Duration) {
	deadline := time.Now().Add(limit)
	for len(l.outbox) > 0 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	time.Sleep(20 * time.Millisecond) // the last frame taken from the queue is being written
}

// Close ends the link. Frames still queued are dropped.
func (l *Link) Close() {
	l.once.Do(func() {
		close(l.done)
		l.conn.Close()
	})
}
