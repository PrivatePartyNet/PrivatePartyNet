package main

// The game network's place among the other network adapters. Games that pick the first network card, and broadcasts to
// 255.255.255.255, go to the adapter with the lowest metric, so the game network takes metric 1 and every other
// connected adapter at 1 (Radmin VPN, for one) steps to 2 while it is on. What each had is recorded first and put back
// exactly afterwards, including whether Windows chose the metric by itself.
//
// The logic is here, on two small interfaces, so it can be tested without changing this PC's adapters; the Windows
// side of both is in netconfig_windows.go.

import (
	"errors"
	"fmt"
)

// adapterState is one IPv4 adapter as Windows reports it.
type adapterState struct {
	LUID      uint64
	Index     int
	Alias     string
	Metric    int
	Automatic bool
	Connected bool
}

// adapters is Windows' list of adapters and the one change made to them.
type adapters interface {
	list() ([]adapterState, error)
	setMetric(luid uint64, metric int, automatic bool) error
}

// records keeps what was moved where only administrators can write it, so it can be put back even after a crash.
type records interface {
	save([]moved) error
	load() []moved
	forget()
}

type moved struct {
	LUID      uint64 `json:"luid"`
	Index     int    `json:"index"`
	Alias     string `json:"alias"`
	Metric    int    `json:"metric"`
	Automatic bool   `json:"automatic"`
}

// sane is true for an entry that could have come from takePriority: an adapter and a metric Windows allows.
func (m moved) sane() bool {
	return m.LUID != 0 && m.Metric >= 0 && m.Metric <= 9999
}

// takePriority moves the other connected adapters at metric 1 to 2, after putting back anything a helper that was
// killed outright left moved. Returns what was moved, and what could not be, in words.
func takePriority(own uint64, w adapters, r records) ([]moved, []string) {
	var problems []string
	if err := restoreLeftovers(w, r); err != nil {
		problems = append(problems, err.Error())
	}
	all, err := w.list()
	if err != nil {
		return nil, append(problems, "the other network adapters could not be read: "+err.Error())
	}
	var list []moved
	for _, a := range all {
		if a.Connected && a.LUID != own && a.Metric <= 1 {
			list = append(list, moved{a.LUID, a.Index, a.Alias, a.Metric, a.Automatic})
		}
	}
	if len(list) == 0 {
		return nil, problems
	}
	if err := r.save(list); err != nil {
		// without the record nothing could be put back after a crash, so nothing is moved
		return nil, append(problems, "nothing was moved, because what to put back could not be recorded: "+err.Error())
	}
	for _, item := range list {
		if err := w.setMetric(item.LUID, 2, false); err != nil {
			problems = append(problems, fmt.Sprintf("%s could not be moved behind the game network: %v", item.Alias, err))
		}
	}
	return list, problems
}

// errGone is what setMetric gives for an adapter that is no longer there (unplugged, or its VPN closed): nothing to
// put back.
var errGone = errors.New("the adapter is gone")

// giveBack puts every adapter in `list` back exactly as it was, then forgets the record.
func giveBack(list []moved, w adapters, r records) error {
	var problems []error
	for _, item := range list {
		if !item.sane() {
			continue
		}
		if err := w.setMetric(item.LUID, item.Metric, item.Automatic); err != nil && !errors.Is(err, errGone) {
			problems = append(problems, fmt.Errorf("%s could not be given its metric back: %w", item.Alias, err))
		}
	}
	r.forget()
	return errors.Join(problems...)
}

// restoreLeftovers gives back what a helper that was killed outright could not.
func restoreLeftovers(w adapters, r records) error {
	if list := r.load(); list != nil {
		return giveBack(list, w, r)
	}
	return nil
}
