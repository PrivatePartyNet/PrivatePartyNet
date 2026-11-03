package main

import (
	"errors"
	"strings"
	"testing"
)

// fakeAdapters is a PC's adapter table in memory.
type fakeAdapters struct {
	all     []adapterState
	failSet map[uint64]bool
	failAll bool
}

func (f *fakeAdapters) list() ([]adapterState, error) {
	if f.failAll {
		return nil, errors.New("Invalid class")
	}
	return append([]adapterState(nil), f.all...), nil
}

func (f *fakeAdapters) setMetric(luid uint64, metric int, automatic bool) error {
	if f.failSet[luid] {
		return errors.New("access is denied")
	}
	for i := range f.all {
		if f.all[i].LUID == luid {
			f.all[i].Automatic = automatic
			if automatic {
				f.all[i].Metric = 25 // what Windows works out by itself
			} else {
				f.all[i].Metric = metric
			}
			return nil
		}
	}
	return errGone
}

func (f *fakeAdapters) get(luid uint64) adapterState {
	for _, a := range f.all {
		if a.LUID == luid {
			return a
		}
	}
	return adapterState{}
}

type fakeRecord struct {
	saved    []moved
	failSave bool
}

func (r *fakeRecord) save(list []moved) error {
	if r.failSave {
		return errors.New("no rights")
	}
	r.saved = append([]moved(nil), list...)
	return nil
}
func (r *fakeRecord) load() []moved { return r.saved }
func (r *fakeRecord) forget()       { r.saved = nil }

const ownLUID = 100

// aPC is the owner's PC as measured: Radmin VPN connected at metric 1 set by hand, Wi-Fi at 35 chosen by Windows, a
// disconnected NordLynx at 1, and the game network's own adapter.
func aPC() *fakeAdapters {
	return &fakeAdapters{all: []adapterState{
		{LUID: 1, Index: 14, Alias: "Radmin VPN", Metric: 1, Automatic: false, Connected: true},
		{LUID: 2, Index: 31, Alias: "Wi-Fi", Metric: 35, Automatic: true, Connected: true},
		{LUID: 3, Index: 39, Alias: "NordLynx", Metric: 1, Automatic: false, Connected: false},
		{LUID: 4, Index: 50, Alias: "Some VPN", Metric: 1, Automatic: true, Connected: true},
		{LUID: ownLUID, Index: 101, Alias: "PrivatePartyNet", Metric: 1, Connected: true},
	}}
}

func TestOnlyConnectedAdaptersAtMetricOneStepAsideAndComeBackExactly(t *testing.T) {
	pc, record := aPC(), &fakeRecord{}
	list, problems := takePriority(ownLUID, pc, record)
	if len(problems) != 0 {
		t.Fatal(problems)
	}
	if len(list) != 2 || pc.get(1).Metric != 2 || pc.get(4).Metric != 2 {
		t.Fatalf("moved %+v; Radmin %+v", list, pc.get(1))
	}
	if pc.get(2).Metric != 35 || pc.get(3).Metric != 1 || pc.get(ownLUID).Metric != 1 {
		t.Fatal("an adapter that was not in the way was moved")
	}
	if len(record.saved) != 2 {
		t.Fatal("what was moved was not recorded first")
	}
	if err := giveBack(list, pc, record); err != nil {
		t.Fatal(err)
	}
	if r := pc.get(1); r.Metric != 1 || r.Automatic {
		t.Fatalf("Radmin came back as %+v, not metric 1 by hand", r)
	}
	if r := pc.get(4); !r.Automatic {
		t.Fatalf("an automatic metric came back as %+v, not automatic", r)
	}
	if record.saved != nil {
		t.Fatal("the record was kept after everything was put back")
	}
}

func TestAfterACrashTheNextStartPutsEverythingBackFirst(t *testing.T) {
	pc, record := aPC(), &fakeRecord{}
	takePriority(ownLUID, pc, record) // ...and then the helper is killed: no giveBack
	if pc.get(1).Metric != 2 {
		t.Fatal("setup")
	}
	if err := restoreLeftovers(pc, record); err != nil {
		t.Fatal(err)
	}
	if pc.get(1).Metric != 1 || pc.get(1).Automatic {
		t.Fatalf("Radmin %+v", pc.get(1))
	}
	if record.saved != nil {
		t.Fatal("the record was kept")
	}
}

func TestAnAdapterThatWentAwayMeanwhileIsNoProblem(t *testing.T) {
	pc, record := aPC(), &fakeRecord{}
	list, _ := takePriority(ownLUID, pc, record)
	pc.all = pc.all[1:] // Radmin VPN was closed while the game network was on
	if err := giveBack(list, pc, record); err != nil {
		t.Fatalf("an adapter that is gone was reported: %v", err)
	}
}

func TestWhatCouldNotBeDoneIsSaidAndNothingIsMovedWithoutARecord(t *testing.T) {
	pc := aPC()
	pc.failSet = map[uint64]bool{1: true}
	_, problems := takePriority(ownLUID, pc, &fakeRecord{})
	if len(problems) != 1 || !strings.Contains(problems[0], "Radmin VPN") {
		t.Fatalf("problems %q", problems)
	}

	pc = aPC()
	list, problems := takePriority(ownLUID, pc, &fakeRecord{failSave: true})
	if list != nil || pc.get(1).Metric != 1 || len(problems) != 1 {
		t.Fatalf("moved without a record: %+v, %q", list, problems)
	}

	pc = aPC()
	pc.failAll = true
	_, problems = takePriority(ownLUID, pc, &fakeRecord{})
	if len(problems) != 1 || !strings.Contains(problems[0], "could not be read") {
		t.Fatalf("a failure to read the adapters was not said: %q", problems)
	}
}

func TestOnlyARecordThatCouldHaveComeFromTakePriorityIsReplayed(t *testing.T) {
	pc := aPC()
	record := &fakeRecord{saved: []moved{{LUID: 1, Metric: -1}, {LUID: 0, Metric: 1}, {LUID: 2, Metric: 99999}}}
	if err := restoreLeftovers(pc, record); err != nil {
		t.Fatal(err)
	}
	if pc.get(1).Metric != 1 || pc.get(2).Metric != 35 {
		t.Fatal("a record that could not have been made by the helper was replayed")
	}
}
