package main

// The Network List Manager, which keeps each network's profile (Public, Private, Domain) for the firewall. Used
// through COM directly: the WMI class behind Get-NetConnectionProfile is missing on some PCs, and this is the
// interface Windows' own Settings app uses. Only the few methods needed are called, by their place in each interface's
// table (netlistmgr.h).

import (
	"errors"
	"fmt"
	"runtime"
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"
)

const (
	categoryPublic  = 0
	categoryPrivate = 1

	// Method numbers, counting IUnknown's 3 and IDispatch's 4 first.
	managerGetNetworkConnections = 9  // INetworkListManager::GetNetworkConnections(IEnumNetworkConnections**)
	enumNext                     = 8  // IEnumNetworkConnections::Next(ULONG, INetworkConnection**, ULONG*)
	connectionGetNetwork         = 7  // INetworkConnection::GetNetwork(INetwork**)
	connectionGetAdapterID       = 12 // INetworkConnection::GetAdapterId(GUID*)
	networkGetCategory           = 18 // INetwork::GetCategory(NLM_NETWORK_CATEGORY*)
	networkSetCategory           = 19 // INetwork::SetCategory(NLM_NETWORK_CATEGORY)
	release                      = 2  // IUnknown::Release

	clsctxAll = 0x17
)

var (
	ole32                = windows.NewLazySystemDLL("ole32.dll")
	procCoCreateInstance = ole32.NewProc("CoCreateInstance")
	clsidNetworkList     = windows.GUID{Data1: 0xDCB00C01, Data2: 0x570F, Data3: 0x4A9B,
		Data4: [8]byte{0x8D, 0x69, 0x19, 0x9F, 0xDB, 0xA5, 0x72, 0x3B}}
	iidNetworkListManager = windows.GUID{Data1: 0xDCB00000, Data2: 0x570F, Data3: 0x4A9B,
		Data4: [8]byte{0x8D, 0x69, 0x19, 0x9F, 0xDB, 0xA5, 0x72, 0x3B}}
)

// call calls method `index` of the COM object `object`. Returns the HRESULT.
func call(object unsafe.Pointer, index int, args ...uintptr) uint32 {
	table := *(*unsafe.Pointer)(object)
	method := *(*uintptr)(unsafe.Add(table, uintptr(index)*unsafe.Sizeof(uintptr(0))))
	result, _, _ := syscall.SyscallN(method, append([]uintptr{uintptr(object)}, args...)...)
	return uint32(result)
}

func failed(hr uint32, what string) error {
	if int32(hr) < 0 {
		return fmt.Errorf("%s (0x%08X)", what, hr)
	}
	return nil
}

// networkCategory gives the profile of the network the adapter `adapter` is on, after setting it to `set` when `set`
// is 0 or more.
func networkCategory(adapter windows.GUID, set int) (int, error) {
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	if err := windows.CoInitializeEx(0, windows.COINIT_MULTITHREADED); err != nil && !errors.Is(err, syscall.Errno(windows.S_FALSE)) {
		return 0, fmt.Errorf("COM could not start: %w", err)
	}
	defer windows.CoUninitialize()
	var manager unsafe.Pointer
	hr, _, _ := procCoCreateInstance.Call(uintptr(unsafe.Pointer(&clsidNetworkList)), 0, clsctxAll,
		uintptr(unsafe.Pointer(&iidNetworkListManager)), uintptr(unsafe.Pointer(&manager)))
	if err := failed(uint32(hr), "the Network List Manager could not be reached"); err != nil {
		return 0, err
	}
	defer call(manager, release)
	connection, err := connectionFor(manager, adapter)
	if err != nil {
		return 0, err
	}
	defer call(connection, release)
	var network unsafe.Pointer
	hr = uintptr(call(connection, connectionGetNetwork, uintptr(unsafe.Pointer(&network))))
	if int32(hr) < 0 || network == nil {
		return 0, fmt.Errorf("the adapter's network could not be read (0x%08X)", uint32(hr))
	}
	defer call(network, release)
	if set >= 0 {
		if err := failed(call(network, networkSetCategory, uintptr(set)), "the network's profile could not be set"); err != nil {
			return 0, err
		}
	}
	var category int32
	if err := failed(call(network, networkGetCategory, uintptr(unsafe.Pointer(&category))),
		"the network's profile could not be read"); err != nil {
		return 0, err
	}
	return int(category), nil
}

// connectionFor finds the Network List Manager's connection on the adapter `adapter`. (GetNetworkConnection wants a
// connection's own id, which is not the adapter's.)
func connectionFor(manager unsafe.Pointer, adapter windows.GUID) (unsafe.Pointer, error) {
	var connections unsafe.Pointer
	if hr := call(manager, managerGetNetworkConnections, uintptr(unsafe.Pointer(&connections))); int32(hr) < 0 || connections == nil {
		return nil, fmt.Errorf("the network connections could not be listed (0x%08X)", hr)
	}
	defer call(connections, release)
	next := func() (windows.GUID, unsafe.Pointer, bool) {
		var connection unsafe.Pointer
		var fetched uint32
		hr := call(connections, enumNext, 1, uintptr(unsafe.Pointer(&connection)), uintptr(unsafe.Pointer(&fetched)))
		if int32(hr) < 0 || fetched == 0 || connection == nil {
			return windows.GUID{}, nil, false
		}
		var id windows.GUID
		call(connection, connectionGetAdapterID, uintptr(unsafe.Pointer(&id))) // on failure id stays all zero: no match
		return id, connection, true
	}
	if found := pickConnection(adapter, next, func(c unsafe.Pointer) { call(c, release) }); found != nil {
		return found, nil
	}
	return nil, errors.New("Windows has no network for the adapter yet")
}

// pickConnection walks the connections `next` gives (each one's adapter id and handle, until ok is false) and returns
// the one on `adapter`, or nil. Every other handle is given to `drop`.
func pickConnection(adapter windows.GUID, next func() (windows.GUID, unsafe.Pointer, bool),
	drop func(unsafe.Pointer)) unsafe.Pointer {
	if adapter == (windows.GUID{}) {
		return nil
	}
	for {
		id, connection, ok := next()
		if !ok {
			return nil
		}
		if id == adapter {
			return connection
		}
		drop(connection)
	}
}
