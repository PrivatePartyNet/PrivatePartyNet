// The game network helper: the small program that holds the game network adapter and runs WireGuard on it. It runs
// with administrator rights (Windows asks once when the game network is turned on), because making a Wintun adapter
// needs them.
//
// It makes the adapter named PrivatePartyNet, gives it this server's address, and runs WireGuard with a peer for each
// member. WireGuard opens no socket: every datagram goes to the app over a loopback connection authenticated with a
// one-time secret, and the app carries it along the best way to that member (see bind.go). Only IPv4 for the game
// network, and broadcast and multicast, passes the adapter (see fanout.go), so it is never a route to anywhere else.
//
// Kill switches, all of them active at once:
//   - the app's loopback connection closes (the app quit, crashed, or turned the game network off);
//   - the app's process is gone (checked every two seconds);
//   - 24 hours have passed.
//
// On the way out it gives back the metrics it moved and closes the adapter, which removes it from Windows.
//
// Started as: PrivatePartyNetGameNet.exe --port P --secret-file F --parent PID
// The secret is read from a file the app writes (and deletes once the helper has connected), so it never appears on a
// command line. The helper only reads that file, and only an ordinary file holding a secret's 64 hex characters: it runs
// with administrator rights, and the file is in a folder any program the user runs could change. The app sends this
// PC's key, address and members over the link once it is open (see link.go).
package main

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"golang.zx2c4.com/wireguard/device"
	"golang.zx2c4.com/wireguard/tun"
	"golang.zx2c4.com/wireguard/windows/tunnel/winipcfg"
)

const (
	AdapterName   = "PrivatePartyNet"
	MTU           = 1320 // a broadcast wrapped for fan-out, then sealed by WireGuard, stays within the 1400 bytes a path carries
	MaxLifetime   = 24 * time.Hour
	ConfigSeconds = 30 // the app sends the first config at once; a helper that never gets one gives up
)

type options struct {
	port          int
	secretFile    string
	parent        int
	memoryAdapter bool
}

func parseOptions(args []string) (options, error) {
	var o options
	flags := flag.NewFlagSet("PrivatePartyNetGameNet", flag.ContinueOnError)
	flags.IntVar(&o.port, "port", 0, "the app's loopback port")
	flags.StringVar(&o.secretFile, "secret-file", "", "the file holding the one-time secret")
	flags.IntVar(&o.parent, "parent", 0, "the app's process id")
	flags.BoolVar(&o.memoryAdapter, "memory-adapter", false, "tests: no Wintun, packets go over the link")
	if err := flags.Parse(args); err != nil {
		return o, err
	}
	if o.port <= 0 || o.port > 65535 || o.secretFile == "" {
		return o, errors.New("--port and --secret-file are needed")
	}
	return o, nil
}

// readSecret reads the one-time secret: 64 hex characters in an ordinary file (not a link to something else).
func readSecret(path string) (string, error) {
	info, err := os.Lstat(path)
	if err != nil {
		return "", err
	}
	if !info.Mode().IsRegular() || info.Size() > 128 {
		return "", errors.New("the secret file is not an ordinary small file")
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	secret := strings.TrimSpace(string(data))
	if raw, err := hex.DecodeString(secret); err != nil || len(raw) != 32 {
		return "", errors.New("the secret file does not hold a secret")
	}
	return secret, nil
}

// AppProof is what the app answers to the helper's secret, to show it is the app that wrote the secret and not some
// other program listening on the port.
func AppProof(secret string) string {
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte("PrivatePartyNet app"))
	return hex.EncodeToString(mac.Sum(nil))
}

func connectToApp(port int, secret string) (net.Conn, error) {
	conn, err := net.DialTimeout("tcp", "127.0.0.1:"+strconv.Itoa(port), 10*time.Second)
	if err != nil {
		return nil, err
	}
	if _, err := conn.Write([]byte(secret + "\n")); err != nil {
		conn.Close()
		return nil, err
	}
	conn.SetReadDeadline(time.Now().Add(10 * time.Second))
	answer := make([]byte, 65)
	if _, err := io.ReadFull(conn, answer); err != nil || !hmac.Equal(answer, []byte(AppProof(secret)+"\n")) {
		conn.Close()
		return nil, errors.New("whatever is on the port is not the app")
	}
	conn.SetReadDeadline(time.Time{})
	if tcp, ok := conn.(*net.TCPConn); ok {
		tcp.SetNoDelay(true)
	}
	return conn, nil
}

func firstConfig(link *Link) (Config, error) {
	var c Config
	link.conn.SetReadDeadline(time.Now().Add(ConfigSeconds * time.Second))
	frame, err := link.Read()
	link.conn.SetReadDeadline(time.Time{})
	if err != nil {
		return c, err
	}
	if frame.Kind != 'C' {
		return c, errors.New("the app did not send the game network's settings")
	}
	err = json.Unmarshal(frame.Body, &c)
	return c, err
}

// notes are the things the app should tell the person: what Windows would not do, in plain words.
type notes struct {
	lock  sync.Mutex
	items []string
}

func (n *notes) add(text string) {
	n.lock.Lock()
	defer n.lock.Unlock()
	n.items = append(n.items, text)
}

func (n *notes) list() []string {
	n.lock.Lock()
	defer n.lock.Unlock()
	return append([]string{}, n.items...)
}

func status(link *Link, state, message string) {
	link.SendJSON('S', map[string]string{"state": state, "message": message})
}

// run is the helper from the open link to the end. Returns why it ended.
func run(link *Link, o options, alive func(int) bool) string {
	config, err := firstConfig(link)
	if err != nil {
		return "no settings from the app: " + err.Error()
	}
	checkedConfig, err := Check(config)
	if err != nil {
		status(link, "failed", err.Error())
		return err.Error()
	}
	var adapter tun.Device
	var memory *MemoryTun
	var warnings notes
	on := func() { link.SendJSON('S', map[string]any{"state": "on", "message": "", "warnings": warnings.list()}) }
	if o.memoryAdapter {
		memory = NewMemoryTun(link)
		adapter = memory
	} else {
		tun.WintunTunnelType = AdapterName
		adapter, err = tun.CreateTUN(AdapterName, MTU)
		if err != nil {
			status(link, "failed", "The game network adapter could not be made: "+err.Error())
			return err.Error()
		}
		luid := winipcfg.LUID(adapter.(*tun.NativeTun).LUID())
		if err := Configure(luid, checkedConfig.self, checkedConfig.network); err != nil {
			adapter.Close()
			status(link, "failed", "The game network adapter could not be set up: "+err.Error())
			return err.Error()
		}
		list, problems := TakePriority(luid)
		defer func() {
			if err := GiveBack(list); err != nil {
				link.Send('L', []byte(err.Error()))
			}
		}()
		for _, problem := range problems {
			link.Send('L', []byte(problem))
		}
		if len(problems) > 0 {
			warnings.add("Another network adapter could not be moved behind the game network, so some games may look " +
				"for players on that one first.")
		}
		// Windows makes the network a few seconds after the address is set, so this waits for it on its own.
		go func() {
			if err := MakePrivate(luid); err != nil {
				link.Send('L', []byte("the game network could not be made Private: "+err.Error()))
				warnings.add("Windows kept this network Public, so games hosted on this PC may be blocked by the firewall.")
				on()
			}
		}()
	}
	logger := &device.Logger{
		Verbosef: device.DiscardLogf,
		Errorf:   func(format string, args ...any) { link.Send('L', []byte(fmt.Sprintf(format, args...))) },
	}
	engine, err := NewEngine(adapter, config, func(peer byte, data []byte) error {
		link.Send('D', []byte{peer}, data)
		return nil
	}, logger)
	if err != nil {
		adapter.Close()
		status(link, "failed", err.Error())
		return err.Error()
	}
	defer engine.Close()
	on()

	var once sync.Once
	why := make(chan string, 3)
	end := func(reason string) { once.Do(func() { why <- reason }) }
	go func() {
		for {
			frame, err := link.Read()
			if err != nil {
				end("the app closed the connection")
				return
			}
			switch frame.Kind {
			case 'D':
				if len(frame.Body) > 1 {
					engine.Deliver(frame.Body[0], frame.Body[1:])
				}
			case 'U':
				if len(frame.Body) == 1 {
					engine.PathUp(frame.Body[0])
				}
			case 'C':
				var next Config
				if err := json.Unmarshal(frame.Body, &next); err == nil {
					err = engine.Apply(next)
				}
				if err != nil {
					link.Send('L', []byte("the new members were not taken: "+err.Error()))
				}
			case 'T':
				if memory != nil {
					memory.Put(frame.Body)
				}
			}
		}
	}()
	go func() {
		started := time.Now()
		for range time.Tick(2 * time.Second) {
			if !alive(o.parent) {
				end("the app is gone")
				return
			}
			if time.Since(started) > MaxLifetime {
				end("the time limit was reached")
				return
			}
		}
	}()
	return <-why
}

func main() {
	o, err := parseOptions(os.Args[1:])
	if err != nil {
		os.Exit(2)
	}
	secret, err := readSecret(o.secretFile)
	if err != nil || secret == "" {
		os.Exit(1)
	}
	conn, err := connectToApp(o.port, secret)
	if err != nil {
		os.Exit(1)
	}
	link := NewLink(conn)
	run(link, o, ParentAlive)
	link.Drain(2 * time.Second) // a last status reaches the app
	link.Close()
}
