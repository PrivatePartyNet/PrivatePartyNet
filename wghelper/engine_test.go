package main

import (
	"bytes"
	"crypto/ecdh"
	"crypto/rand"
	"encoding/binary"
	"encoding/hex"
	"net/netip"
	"strings"
	"sync"
	"testing"
	"time"

	"golang.zx2c4.com/wireguard/device"
	"golang.zx2c4.com/wireguard/tun/tuntest"
)

const testNetwork = "10.77.12.0/24"

type keypair struct{ private, public string }

func newKeypair(t *testing.T) keypair {
	key, err := ecdh.X25519().GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	return keypair{hex.EncodeToString(key.Bytes()), hex.EncodeToString(key.PublicKey().Bytes())}
}

// udpPacket is an IPv4 UDP packet with a correct header checksum.
func udpPacket(source, destination string, payload []byte) []byte {
	total := ipHeader + udpHeader + len(payload)
	out := make([]byte, total)
	out[0], out[8], out[9] = 0x45, 64, 17
	binary.BigEndian.PutUint16(out[2:4], uint16(total))
	s, d := netip.MustParseAddr(source).As4(), netip.MustParseAddr(destination).As4()
	copy(out[12:16], s[:])
	copy(out[16:20], d[:])
	binary.BigEndian.PutUint16(out[10:12], checksum(out[:ipHeader]))
	binary.BigEndian.PutUint16(out[20:22], 5000)
	binary.BigEndian.PutUint16(out[22:24], 5000)
	binary.BigEndian.PutUint16(out[24:26], uint16(udpHeader+len(payload)))
	copy(out[28:], payload)
	return out
}

// member is one PC: an engine on an in-memory adapter.
type member struct {
	address string
	keys    keypair
	tun     *tuntest.ChannelTUN
	engine  *Engine
}

// mesh links engines as the app would: each datagram for member N is handed to N's engine, tagged with the sender.
type testMesh struct {
	lock    sync.Mutex
	members map[byte]*member
	waiting map[byte][]datagram // for members whose engine is not made yet, as a path would hold them
}

func (m *testMesh) sender(from byte) func(byte, []byte) error {
	return func(to byte, data []byte) error {
		m.lock.Lock()
		defer m.lock.Unlock()
		copied := append([]byte(nil), data...)
		if target := m.members[to]; target != nil && target.engine != nil {
			target.engine.Deliver(from, copied)
		} else {
			m.waiting[to] = append(m.waiting[to], datagram{from, copied})
		}
		return nil
	}
}

func configFor(self *member, everyone []*member) Config {
	c := Config{PrivateKey: self.keys.private, Address: self.address, Network: testNetwork}
	for _, other := range everyone {
		if other != self {
			c.Peers = append(c.Peers, Peer{Key: other.keys.public, Address: other.address})
		}
	}
	return c
}

func startMesh(t *testing.T, addresses ...string) (*testMesh, []*member) {
	m := &testMesh{members: map[byte]*member{}, waiting: map[byte][]datagram{}}
	var everyone []*member
	for _, address := range addresses {
		everyone = append(everyone, &member{address: address, keys: newKeypair(t), tun: tuntest.NewChannelTUN()})
	}
	for _, p := range everyone {
		last := netip.MustParseAddr(p.address).As4()[3]
		m.lock.Lock()
		m.members[last] = p
		m.lock.Unlock()
		engine, err := NewEngine(p.tun.TUN(), configFor(p, everyone), m.sender(last),
			device.NewLogger(device.LogLevelError, p.address+" "))
		if err != nil {
			t.Fatal(err)
		}
		m.lock.Lock()
		p.engine = engine
		for _, d := range m.waiting[last] {
			engine.Deliver(d.peer, d.data)
		}
		delete(m.waiting, last)
		m.lock.Unlock()
		t.Cleanup(engine.Close)
	}
	// Wait for every pair to finish its first handshake, as the members of a running game network have.
	deadline := time.Now().Add(10 * time.Second)
	for _, p := range everyone {
		for time.Now().Before(deadline) {
			stats, _ := p.engine.Stats()
			if strings.Count(stats, "last_handshake_time_sec=") == len(everyone)-1 &&
				!strings.Contains(stats, "last_handshake_time_sec=0\n") {
				break
			}
			time.Sleep(10 * time.Millisecond)
		}
	}
	return m, everyone
}

func receive(t *testing.T, p *member, within time.Duration) []byte {
	t.Helper()
	select {
	case packet := <-p.tun.Inbound:
		return packet
	case <-time.After(within):
		return nil
	}
}

func TestUnicastGoesToOneMemberOnly(t *testing.T) {
	_, ms := startMesh(t, "10.77.12.1", "10.77.12.2", "10.77.12.3")
	ms[0].tun.Outbound <- udpPacket("10.77.12.1", "10.77.12.2", []byte("hello two"))
	got := receive(t, ms[1], 5*time.Second)
	if got == nil || !bytes.HasSuffix(got, []byte("hello two")) {
		t.Fatalf("member two got %x", got)
	}
	if extra := receive(t, ms[2], 300*time.Millisecond); extra != nil {
		t.Fatalf("member three got a packet that was not for it: %x", extra)
	}
}

func TestBroadcastAndMulticastReachEveryMember(t *testing.T) {
	_, ms := startMesh(t, "10.77.12.1", "10.77.12.2", "10.77.12.3")
	for _, destination := range []string{"10.77.12.255", "255.255.255.255", "239.255.255.250"} {
		packet := udpPacket("10.77.12.1", destination, []byte("anyone there? "+destination))
		ms[0].tun.Outbound <- packet
		for _, other := range ms[1:] {
			got := receive(t, other, 5*time.Second)
			if !bytes.Equal(got, packet) {
				t.Fatalf("%s: %s got %x, want the original packet", destination, other.address, got)
			}
		}
		if echo := receive(t, ms[0], 200*time.Millisecond); echo != nil {
			t.Fatalf("the sender got its own broadcast back")
		}
	}
}

func TestOnlyGameNetworkTrafficLeaves(t *testing.T) {
	_, ms := startMesh(t, "10.77.12.1", "10.77.12.2")
	ms[0].tun.Outbound <- udpPacket("10.77.12.1", "8.8.8.8", []byte("to the internet"))
	ms[0].tun.Outbound <- udpPacket("10.77.12.1", "192.168.1.10", []byte("to a home network"))
	ms[0].tun.Outbound <- udpPacket("10.77.12.9", "10.77.12.2", []byte("from another address"))
	ms[0].tun.Outbound <- udpPacket("10.77.12.1", "10.77.12.2", []byte("game"))
	got := receive(t, ms[1], 5*time.Second)
	if !bytes.HasSuffix(got, []byte("game")) {
		t.Fatalf("got %x, want only the game packet", got)
	}
	if extra := receive(t, ms[1], 300*time.Millisecond); extra != nil {
		t.Fatalf("something else got through: %x", extra)
	}
}

func TestAWrappedBroadcastInSomeoneElsesNameIsDropped(t *testing.T) {
	_, ms := startMesh(t, "10.77.12.1", "10.77.12.2", "10.77.12.3")
	// Member 1 sends, as an ordinary packet to member 2, a wrapper whose inner broadcast claims to come from member 3.
	forged := Wrap(udpPacket("10.77.12.3", "10.77.12.255", []byte("I am three")), netip.MustParseAddr("10.77.12.1"),
		netip.MustParseAddr("10.77.12.2"))
	ms[0].tun.Outbound <- forged
	ms[0].tun.Outbound <- udpPacket("10.77.12.1", "10.77.12.2", []byte("marker"))
	got := receive(t, ms[1], 5*time.Second)
	if !bytes.HasSuffix(got, []byte("marker")) {
		t.Fatalf("got %x, want the forged broadcast dropped and the marker through", got)
	}
}

func TestARemovedMemberIsCutOffAtOnce(t *testing.T) {
	_, ms := startMesh(t, "10.77.12.1", "10.77.12.2", "10.77.12.3")
	ms[2].tun.Outbound <- udpPacket("10.77.12.3", "10.77.12.1", []byte("before"))
	if got := receive(t, ms[0], 5*time.Second); !bytes.HasSuffix(got, []byte("before")) {
		t.Fatalf("got %x before removal", got)
	}
	// Members 1 and 2 hear that 3 was banned; 3 still has its old config and keeps sending.
	if err := ms[0].engine.Apply(configFor(ms[0], ms[:2])); err != nil {
		t.Fatal(err)
	}
	if err := ms[1].engine.Apply(configFor(ms[1], ms[:2])); err != nil {
		t.Fatal(err)
	}
	ms[2].tun.Outbound <- udpPacket("10.77.12.3", "10.77.12.1", []byte("after"))
	ms[2].tun.Outbound <- udpPacket("10.77.12.3", "10.77.12.255", []byte("after, to everyone"))
	if got := receive(t, ms[0], time.Second); got != nil {
		t.Fatalf("a removed member still got through: %x", got)
	}
	ms[1].tun.Outbound <- udpPacket("10.77.12.2", "10.77.12.1", []byte("still here"))
	if got := receive(t, ms[0], 5*time.Second); !bytes.HasSuffix(got, []byte("still here")) {
		t.Fatalf("the member who stayed lost the link: %x", got)
	}
}

func TestCheckRefusesAnythingOutsideTheGameNetwork(t *testing.T) {
	keys := newKeypair(t)
	good := Config{PrivateKey: keys.private, Address: "10.77.12.1", Network: testNetwork,
		Peers: []Peer{{Key: newKeypair(t).public, Address: "10.77.12.2"}}}
	if _, err := Check(good); err != nil {
		t.Fatal(err)
	}
	bad := []func(c *Config){
		func(c *Config) { c.Network = "0.0.0.0/0" },
		func(c *Config) { c.Network = "192.168.1.0/24" },
		func(c *Config) { c.Network = "10.77.0.0/16" },
		func(c *Config) { c.Address = "10.77.13.1" },
		func(c *Config) { c.Address = "10.77.12.255" },
		func(c *Config) { c.PrivateKey = "abcd" },
		func(c *Config) { c.Peers = []Peer{{Key: keys.public, Address: "8.8.8.8"}} },
		func(c *Config) { c.Peers = []Peer{{Key: keys.public, Address: "10.77.12.1"}} },
		func(c *Config) { c.Peers = append(c.Peers, Peer{Key: c.Peers[0].Key, Address: "10.77.12.3"}) },
	}
	for i, change := range bad {
		c := good
		c.Peers = append([]Peer(nil), good.Peers...)
		change(&c)
		if _, err := Check(c); err == nil {
			t.Errorf("case %d was accepted: %+v", i, c)
		}
	}
}

func TestDatagramsForUnknownOrRemovedMembersAreDroppedQuietly(t *testing.T) {
	_, ms := startMesh(t, "10.77.12.1", "10.77.12.2", "10.77.12.3")
	if err := ms[0].engine.Apply(configFor(ms[0], ms[:2])); err != nil { // member 3 removed
		t.Fatal(err)
	}
	ms[0].engine.Deliver(3, []byte{4, 0, 0, 0, 1, 2, 3}) // from the removed member: a cut-short data message
	ms[0].engine.Deliver(99, make([]byte, 148))          // from nobody: a handshake-sized datagram of zeros
	ms[0].engine.Deliver(2, nil)
	ms[0].engine.PathUp(3)
	ms[0].engine.PathUp(200)
	ms[1].tun.Outbound <- udpPacket("10.77.12.2", "10.77.12.1", []byte("still fine"))
	if got := receive(t, ms[0], 5*time.Second); !bytes.HasSuffix(got, []byte("still fine")) {
		t.Fatalf("got %x", got)
	}
}

func tcpPacket(source, destination string, port uint16) []byte {
	packet := udpPacket(source, destination, make([]byte, 12))
	packet[9] = 6
	binary.BigEndian.PutUint16(packet[10:12], 0)
	binary.BigEndian.PutUint16(packet[10:12], checksum(packet[:ipHeader]))
	binary.BigEndian.PutUint16(packet[22:24], port)
	return packet
}

func TestWindowsSharingAndRemoteDesktopAreClosedToMembers(t *testing.T) {
	_, ms := startMesh(t, "10.77.12.1", "10.77.12.2")
	for _, port := range []uint16{445, 139, 3389, 5985} {
		ms[0].tun.Outbound <- tcpPacket("10.77.12.1", "10.77.12.2", port)
	}
	nbns := udpPacket("10.77.12.1", "10.77.12.255", []byte("WORKGROUP"))
	binary.BigEndian.PutUint16(nbns[22:24], 137)
	ms[0].tun.Outbound <- nbns
	ms[0].tun.Outbound <- tcpPacket("10.77.12.1", "10.77.12.2", 27015) // a game server's port
	got := receive(t, ms[1], 5*time.Second)
	if got == nil || binary.BigEndian.Uint16(got[22:24]) != 27015 {
		t.Fatalf("got %x, want only the game's packet", got)
	}
	if extra := receive(t, ms[1], 300*time.Millisecond); extra != nil {
		t.Fatalf("a closed port got through: %x", extra)
	}
}

func TestAFragmentIsNeverTakenForAWrapper(t *testing.T) {
	mesh := &Mesh{Self: netip.MustParseAddr("10.77.12.2"), Network: netip.MustParsePrefix(testNetwork)}
	wrapper := Wrap(udpPacket("10.77.12.1", "10.77.12.255", []byte("hi")), netip.MustParseAddr("10.77.12.1"), mesh.Self)
	if inner, wrapped := Unwrap(wrapper, netip.MustParseAddr("10.77.12.1"), mesh); !wrapped || inner == nil {
		t.Fatal("a whole wrapper was not opened")
	}
	binary.BigEndian.PutUint16(wrapper[6:8], 0x2000) // more fragments follow
	if _, wrapped := Unwrap(wrapper, netip.MustParseAddr("10.77.12.1"), mesh); wrapped {
		t.Fatal("a fragment was taken for a wrapper")
	}
}
