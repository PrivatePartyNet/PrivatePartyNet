// Voice for one server: the microphone, who hears whom, mute and deafen, push-to-talk, device choice and a "who is
// speaking" signal. Audio travels peer to peer on the connections the screens already use (rtc.js gives each pair one
// audio slot). Nothing is sent to anyone outside the voice channel you are in: a peer's slot carries your microphone
// only while both of you are in the same voice channel, and their sound plays only then.
(function () {
  "use strict";

  var SPEAKING_LEVEL = 0.02; // loudness (0 to 1) above which a person counts as speaking
  var SAMPLE_MS = 100; // how often the speaking light is checked: 10 times a second is plenty and costs almost nothing
  var HANG_MS = 400; // a speaking light stays on this long after the sound stops, so it does not flicker between words
  var KEY_PREF = "privatepartynet.voice.";

  function create(options) {
    var RTC = options.rtc;
    var el = options.el;
    var state = {
      channel: null, // the voice channel this window is in, or null
      stream: null,
      track: null,
      muted: false,
      deafened: false,
      ptt: false,
      pttKey: "KeyV",
      pttDown: false,
      inputId: "",
      outputId: "",
      volumes: new Map(),
    };
    var remotes = new Map(); // peer id -> { audio, source, analyser, stream }
    var context = null;
    var local = null; // { source, analyser }
    var timer = null;
    var lastLoud = new Map(); // id -> time it was last loud
    var speaking = new Set();

    function pref(name, fallback) {
      try {
        var value = localStorage.getItem(KEY_PREF + name);
        return value === null ? fallback : value;
      } catch (err) {
        return fallback;
      }
    }

    function savePref(name, value) {
      try {
        localStorage.setItem(KEY_PREF + name, String(value));
      } catch (err) {
        // not saved; the choice still applies for this visit
      }
    }

    state.ptt = pref("ptt", "off") === "on";
    state.inputId = pref("input", "");
    state.outputId = pref("output", "");
    state.pttKey = pref("pttKey", "KeyV");

    // --- the microphone -----------------------------------------------------------------------------------

    function constraints() {
      return {
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1,
          sampleRate: 48000,
          deviceId: state.inputId ? { exact: state.inputId } : undefined,
        },
        video: false,
      };
    }

    async function openMic() {
      var stream;
      try {
        stream = await navigator.mediaDevices.getUserMedia(constraints());
      } catch (err) {
        if (state.inputId && err && (err.name === "OverconstrainedError" || err.name === "NotFoundError")) {
          state.inputId = ""; // the saved microphone is not there any more; use the default one
          savePref("input", "");
          stream = await navigator.mediaDevices.getUserMedia(constraints());
        } else {
          throw err;
        }
      }
      return stream;
    }

    function micMessage(err) {
      if (err && (err.name === "NotAllowedError" || err.name === "SecurityError")) {
        return "PrivatePartyNet is not allowed to use the microphone. Allow it in Windows privacy settings or in the window's prompt.";
      }
      if (err && err.name === "NotFoundError") return "No microphone was found.";
      return "Could not start the microphone: " + (err && err.message ? err.message : err);
    }

    function audioContext() {
      if (!context) context = new (window.AudioContext || window.webkitAudioContext)();
      if (context.state === "suspended") context.resume().catch(function () {});
      return context;
    }

    function applyEnabled() {
      if (!state.track) return;
      var talking = !state.muted && !state.deafened && (!state.ptt || state.pttDown);
      state.track.enabled = talking;
      renderBar();
    }

    // --- joining and leaving ---------------------------------------------------------------------------------

    async function join(channelId, channelName) {
      if (state.channel === channelId) return;
      if (state.channel) stopLocal();
      try {
        state.stream = await openMic();
      } catch (err) {
        options.notice(micMessage(err));
        return;
      }
      state.track = state.stream.getAudioTracks()[0];
      state.channel = channelId;
      state.channelName = channelName || "";
      var ctx = audioContext();
      local = { source: ctx.createMediaStreamSource(state.stream), analyser: ctx.createAnalyser() };
      local.analyser.fftSize = 512;
      local.source.connect(local.analyser);
      applyEnabled();
      startTimer();
      refreshDevices();
      state.confirmed = false;
      try {
        await options.post("/voice", { channel: channelId });
      } catch (err) {
        stopLocal();
        options.notice("Could not join that voice channel. It may be full.");
        return;
      }
      sync();
      renderBar();
    }

    function leave() {
      var was = state.channel;
      stopLocal();
      if (was) options.post("/voice", { channel: null }).catch(function () {});
    }

    // Stop using the microphone and drop every voice connection, without telling the server (it already knows, or the
    // window is closing).
    function stopLocal() {
      stopTimer();
      if (state.track) state.track.stop();
      if (state.stream) state.stream.getTracks().forEach(function (t) { t.stop(); });
      if (local) {
        try { local.source.disconnect(); } catch (err) { /* already gone */ }
        local = null;
      }
      state.stream = null;
      state.track = null;
      state.channel = null;
      state.channelName = "";
      state.confirmed = false;
      options.peers().forEach(function (entry) { RTC.setMicTrack(entry.pc, null).catch(function () {}); });
      remotes.forEach(function (remote, id) { removeRemote(id); });
      speaking = new Set();
      options.onSpeaking(speaking);
      renderBar();
    }

    // --- who hears whom --------------------------------------------------------------------------------------

    function removeRemote(id) {
      var remote = remotes.get(id);
      if (!remote) return;
      try { remote.source.disconnect(); } catch (err) { /* already gone */ }
      remote.audio.pause();
      remote.audio.srcObject = null;
      remote.audio.remove();
      remotes.delete(id);
    }

    // Called whenever the roster or the set of connections changes. Puts the microphone into the connections of the
    // people who are in my voice channel, takes it out of everyone else's, and plays only their voices.
    function sync() {
      var members = options.members();
      var peers = options.peers();
      var mine = members.get(options.meId());
      if (state.channel && mine) {
        // A roster that was already on its way when I joined may still say I am in no channel. Only a roster that has
        // once shown me in this channel and then stops showing me there means the server moved me out (I became a
        // guest, or the channel was removed).
        if (mine.voice === state.channel) state.confirmed = true;
        else if (state.confirmed) {
          stopLocal();
          return;
        }
      }
      peers.forEach(function (entry, id) {
        var person = members.get(id);
        var together = !!(state.channel && person && person.voice === state.channel && person.online);
        RTC.setMicTrack(entry.pc, together && state.track ? state.track : null).catch(function () {});
        if (!together) {
          removeRemote(id);
          return;
        }
        var track = RTC.remoteAudioTrack(entry.pc);
        if (!track) return;
        var remote = remotes.get(id);
        if (!remote || remote.track !== track) {
          removeRemote(id);
          var stream = new MediaStream([track]);
          var audio = document.createElement("audio");
          audio.autoplay = true;
          audio.srcObject = stream;
          audio.style.display = "none";
          document.body.append(audio);
          var ctx = audioContext();
          var source = ctx.createMediaStreamSource(stream);
          var analyser = ctx.createAnalyser();
          analyser.fftSize = 512;
          source.connect(analyser);
          remote = { audio: audio, source: source, analyser: analyser, stream: stream, track: track };
          remotes.set(id, remote);
          if (state.outputId && audio.setSinkId) audio.setSinkId(state.outputId).catch(function () {});
        }
        remote.audio.muted = state.deafened;
        remote.audio.volume = state.volumes.has(id) ? state.volumes.get(id) : 1;
        remote.audio.play().catch(function () {});
      });
      remotes.forEach(function (remote, id) {
        if (!peers.has(id)) removeRemote(id);
      });
      renderBar();
    }

    // --- who is speaking -------------------------------------------------------------------------------------

    function level(analyser) {
      var data = new Uint8Array(analyser.fftSize);
      analyser.getByteTimeDomainData(data);
      var sum = 0;
      for (var i = 0; i < data.length; i += 1) {
        var v = (data[i] - 128) / 128;
        sum += v * v;
      }
      return Math.sqrt(sum / data.length);
    }

    function startTimer() {
      if (timer) return;
      timer = setInterval(function () {
        var now = performance.now();
        var next = new Set();
        if (local && state.track && state.track.enabled && level(local.analyser) > SPEAKING_LEVEL) lastLoud.set(options.meId(), now);
        remotes.forEach(function (remote, id) {
          if (!remote.audio.muted && level(remote.analyser) > SPEAKING_LEVEL) lastLoud.set(id, now);
        });
        lastLoud.forEach(function (when, id) {
          if (now - when < HANG_MS) next.add(id);
        });
        var changed = next.size !== speaking.size || Array.from(next).some(function (id) { return !speaking.has(id); });
        speaking = next;
        if (changed) options.onSpeaking(speaking);
      }, SAMPLE_MS);
    }

    function stopTimer() {
      if (timer) clearInterval(timer);
      timer = null;
      lastLoud.clear();
    }

    // --- mute, deafen, push to talk ---------------------------------------------------------------------------

    function setMuted(on) {
      state.muted = on;
      applyEnabled();
    }

    function setDeafened(on) {
      state.deafened = on;
      remotes.forEach(function (remote) { remote.audio.muted = on; });
      applyEnabled();
    }

    function setPtt(on) {
      state.ptt = on;
      state.pttDown = false;
      savePref("ptt", on ? "on" : "off");
      applyEnabled();
    }

    function typing(event) {
      var tag = event.target && event.target.tagName;
      return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (event.target && event.target.isContentEditable);
    }

    document.addEventListener("keydown", function (event) {
      if (state.ptt && state.channel && event.code === state.pttKey && !typing(event) && !event.repeat) {
        state.pttDown = true;
        applyEnabled();
      }
    });
    document.addEventListener("keyup", function (event) {
      if (state.ptt && event.code === state.pttKey && state.pttDown) {
        state.pttDown = false;
        applyEnabled();
      }
    });
    window.addEventListener("blur", function () {
      if (state.pttDown) {
        state.pttDown = false;
        applyEnabled();
      }
    });

    // --- devices ---------------------------------------------------------------------------------------------

    async function refreshDevices() {
      if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
      var devices = await navigator.mediaDevices.enumerateDevices().catch(function () { return []; });
      fill(el.inputSel, devices.filter(function (d) { return d.kind === "audioinput"; }), state.inputId);
      fill(el.outputSel, devices.filter(function (d) { return d.kind === "audiooutput"; }), state.outputId);
    }

    function fill(select, list, chosen) {
      select.textContent = "";
      var first = document.createElement("option");
      first.value = "";
      first.textContent = "Default";
      select.append(first);
      list.forEach(function (device, index) {
        if (device.deviceId === "default" || device.deviceId === "communications") return;
        var option = document.createElement("option");
        option.value = device.deviceId;
        option.textContent = device.label || (select === el.inputSel ? "Microphone " : "Speakers ") + (index + 1);
        select.append(option);
      });
      select.value = Array.from(select.options).some(function (o) { return o.value === chosen; }) ? chosen : "";
    }

    el.inputSel.addEventListener("change", async function () {
      state.inputId = el.inputSel.value;
      savePref("input", state.inputId);
      if (!state.channel) return;
      try {
        var stream = await openMic();
        var old = state.stream;
        state.stream = stream;
        state.track = stream.getAudioTracks()[0];
        if (local) {
          try { local.source.disconnect(); } catch (err) { /* already gone */ }
          local.source = audioContext().createMediaStreamSource(stream);
          local.source.connect(local.analyser);
        }
        old.getTracks().forEach(function (t) { t.stop(); });
        applyEnabled();
        sync();
      } catch (err) {
        options.notice(micMessage(err));
      }
    });

    el.outputSel.addEventListener("change", function () {
      state.outputId = el.outputSel.value;
      savePref("output", state.outputId);
      remotes.forEach(function (remote) {
        if (remote.audio.setSinkId) remote.audio.setSinkId(state.outputId).catch(function () {});
      });
    });

    el.pttChk.checked = state.ptt;
    el.pttChk.addEventListener("change", function () { setPtt(el.pttChk.checked); });

    // --- the bar ---------------------------------------------------------------------------------------------

    function renderBar() {
      el.bar.hidden = !state.channel;
      if (!state.channel) return;
      el.name.textContent = state.channelName || "Voice";
      el.muteBtn.textContent = state.muted ? "Unmute" : "Mute";
      el.muteBtn.classList.toggle("on", state.muted);
      el.deafenBtn.textContent = state.deafened ? "Undeafen" : "Deafen";
      el.deafenBtn.classList.toggle("on", state.deafened);
      el.state.textContent = state.deafened ? "Deafened" : state.muted ? "Muted" :
        state.ptt ? (state.pttDown ? "Talking" : "Hold " + state.pttKey.replace("Key", "") + " to talk") : "Live";
      renderVolumes();
    }

    function renderVolumes() {
      el.volumes.textContent = "";
      var members = options.members();
      members.forEach(function (person, id) {
        if (id === options.meId() || person.voice !== state.channel || person.role === "viewer") return;
        var row = document.createElement("label");
        row.className = "volume-row";
        var name = document.createElement("span");
        name.textContent = person.name;
        var slider = document.createElement("input");
        slider.type = "range";
        slider.min = "0";
        slider.max = "100";
        slider.value = String(Math.round((state.volumes.has(id) ? state.volumes.get(id) : 1) * 100));
        slider.setAttribute("aria-label", "Volume for " + person.name);
        slider.addEventListener("input", function () {
          state.volumes.set(id, Number(slider.value) / 100);
          var remote = remotes.get(id);
          if (remote) remote.audio.volume = Number(slider.value) / 100;
        });
        row.append(name, slider);
        el.volumes.append(row);
      });
    }

    el.muteBtn.addEventListener("click", function () { setMuted(!state.muted); });
    el.deafenBtn.addEventListener("click", function () { setDeafened(!state.deafened); });
    el.leaveBtn.addEventListener("click", leave);
    el.settingsBtn.addEventListener("click", function () {
      el.panel.hidden = !el.panel.hidden;
      if (!el.panel.hidden) refreshDevices();
    });
    if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
      navigator.mediaDevices.addEventListener("devicechange", function () { if (!el.panel.hidden) refreshDevices(); });
    }

    return {
      join: join,
      leave: leave,
      stopLocal: stopLocal,
      sync: sync,
      channel: function () { return state.channel; },
      speaking: function () { return speaking; },
      state: state,
    };
  }

  var api = { create: create };
  if (typeof window !== "undefined") window.PartyVoice = api;
  if (typeof module === "object" && module.exports) module.exports = api;
})();
