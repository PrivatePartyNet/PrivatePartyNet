package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// app is the app's end of one helper's link, run in the test.
type app struct {
	conn   net.Conn
	frames chan Frame
}

func startHelper(t *testing.T, alive func(int) bool) (*app, chan string) {
	appSide, helperSide := net.Pipe()
	a := &app{conn: appSide, frames: make(chan Frame, 256)}
	go func() {
		for {
			frame, err := ReadFrame(appSide)
			if err != nil {
				close(a.frames)
				return
			}
			a.frames <- frame
		}
	}()
	why := make(chan string, 1)
	o := options{memoryAdapter: true}
	go func() {
		link := NewLink(helperSide)
		why <- run(link, o, alive)
		link.Drain(time.Second)
		link.Close()
	}()
	t.Cleanup(func() { appSide.Close() })
	return a, why
}

func (a *app) send(kind byte, body ...[]byte) {
	a.conn.Write(encode(kind, body...))
}

func (a *app) sendConfig(c Config) {
	data, _ := json.Marshal(c)
	a.send('C', data)
}

// next is the next frame of `kind`, skipping the others.
func (a *app) next(t *testing.T, kind byte) Frame {
	t.Helper()
	deadline := time.After(5 * time.Second)
	for {
		select {
		case frame, ok := <-a.frames:
			if !ok {
				t.Fatalf("the helper closed the link while a %q frame was awaited", kind)
			}
			if frame.Kind == kind {
				return frame
			}
		case <-deadline:
			t.Fatalf("no %q frame", kind)
		}
	}
}

func TestTwoHelpersCarryPacketsThroughTheirApps(t *testing.T) {
	one, two := newKeypair(t), newKeypair(t)
	a, _ := startHelper(t, func(int) bool { return true })
	b, _ := startHelper(t, func(int) bool { return true })
	a.sendConfig(Config{PrivateKey: one.private, Address: "10.77.12.1", Network: testNetwork,
		Peers: []Peer{{Key: two.public, Address: "10.77.12.2"}}})
	b.sendConfig(Config{PrivateKey: two.private, Address: "10.77.12.2", Network: testNetwork,
		Peers: []Peer{{Key: one.public, Address: "10.77.12.1"}}})
	for _, side := range []*app{a, b} {
		if state := side.next(t, 'S'); !strings.Contains(string(state.Body), `"state":"on"`) {
			t.Fatalf("status %s", state.Body)
		}
	}
	// Each app hands the other's datagrams over, tagged with the member they came from, as PeerPath would.
	packets := make(chan []byte, 8)
	pump := func(from, to *app, sender byte) {
		for frame := range from.frames {
			switch frame.Kind {
			case 'D':
				to.send('D', []byte{sender}, frame.Body[1:])
			case 'T':
				packets <- frame.Body
			}
		}
	}
	go pump(a, b, 1)
	go pump(b, a, 2)
	a.send('U', []byte{2})
	want := udpPacket("10.77.12.1", "10.77.12.255", []byte("anyone for a game?"))
	a.send('T', want)
	select {
	case got := <-packets:
		if !bytes.Equal(got, want) {
			t.Fatalf("got %x", got)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("the broadcast never arrived")
	}
}

func TestTheHelperEndsWhenTheAppGoes(t *testing.T) {
	keys := newKeypair(t)
	config := Config{PrivateKey: keys.private, Address: "10.77.12.1", Network: testNetwork}

	a, why := startHelper(t, func(int) bool { return true })
	a.sendConfig(config)
	a.next(t, 'S')
	a.conn.Close()
	if reason := <-why; reason != "the app closed the connection" {
		t.Fatalf("ended because %q", reason)
	}

	gone := make(chan bool, 1)
	b, why := startHelper(t, func(int) bool { return len(gone) == 0 })
	b.sendConfig(config)
	b.next(t, 'S')
	gone <- true
	select {
	case reason := <-why:
		if reason != "the app is gone" {
			t.Fatalf("ended because %q", reason)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("the helper outlived the app")
	}
}

func TestSettingsOutsideTheGameNetworkAreRefused(t *testing.T) {
	keys := newKeypair(t)
	a, why := startHelper(t, func(int) bool { return true })
	a.sendConfig(Config{PrivateKey: keys.private, Address: "192.168.1.5", Network: "192.168.1.0/24"})
	if state := a.next(t, 'S'); !strings.Contains(string(state.Body), `"state":"failed"`) {
		t.Fatalf("status %s", state.Body)
	}
	<-why
}

func TestTheSecretIsOnlyTakenFromAnOrdinaryFileThatHoldsOne(t *testing.T) {
	folder := t.TempDir()
	good := filepath.Join(folder, "net.key")
	secret := strings.Repeat("ab", 32)
	os.WriteFile(good, []byte(secret), 0o600)
	if got, err := readSecret(good); err != nil || got != secret {
		t.Fatalf("got %q, %v", got, err)
	}
	if _, err := os.Stat(good); err != nil {
		t.Fatal("the helper must not delete it: the app does")
	}
	other := filepath.Join(folder, "something.txt")
	os.WriteFile(other, []byte("password=hunter2"), 0o600)
	if _, err := readSecret(other); err == nil {
		t.Fatal("a file without a secret in it was read as one")
	}
	if _, err := readSecret(folder); err == nil {
		t.Fatal("a folder was read as a secret")
	}
	link := filepath.Join(folder, "link.key")
	if os.Symlink(good, link) == nil { // needs Developer Mode or administrator rights to make
		if _, err := readSecret(link); err == nil {
			t.Fatal("a link was followed")
		}
	}
}

func TestTheHelperOnlyTalksToTheAppThatWroteTheSecret(t *testing.T) {
	secret := strings.Repeat("cd", 32)
	for _, answer := range []string{AppProof(secret), AppProof(strings.Repeat("ef", 32))} {
		listener, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		go func() {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			line := make([]byte, 65)
			if _, err := io.ReadFull(conn, line); err == nil {
				conn.Write([]byte(answer + "\n"))
			}
			time.Sleep(200 * time.Millisecond)
			conn.Close()
		}()
		conn, err := connectToApp(listener.Addr().(*net.TCPAddr).Port, secret)
		listener.Close()
		if answer == AppProof(secret) && err != nil {
			t.Fatalf("the real app was refused: %v", err)
		}
		if answer != AppProof(secret) && err == nil {
			t.Fatal("a program that does not know the secret was taken for the app")
		}
		if conn != nil {
			conn.Close()
		}
	}
}
