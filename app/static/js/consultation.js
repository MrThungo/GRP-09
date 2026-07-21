(function () {
  const appUrl = (path) => {
    const value = String(path || "");
    return value && window.nmbUrl ? window.nmbUrl(value) : value;
  };

  const postJson = async (url, body) => {
    const response = await fetch(appUrl(url), {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error("Request failed");
    return response;
  };

  const waiting = document.querySelector("[data-consultation-waiting]");
  if (waiting) {
    const statusUrl = appUrl(waiting.dataset.statusUrl || "");
    const signalsUrl = appUrl(waiting.dataset.signalsUrl || "");
    const joinUrl = appUrl(waiting.dataset.joinUrl || "");
    const statusText = waiting.querySelector("[data-consultation-waiting-status]");

    const pingWaitingRoom = async () => {
      if (!signalsUrl) return;
      try {
        await postJson(signalsUrl, {
          type: "waiting",
          payload: { at: new Date().toISOString() },
        });
      } catch (error) {
        /* The doctor will still see the next successful check-in ping. */
      }
    };

    const poll = async () => {
      try {
        const response = await fetch(statusUrl, {
          credentials: "same-origin",
          cache: "no-store",
          headers: { Accept: "application/json" },
        });
        if (!response.ok) return;
        const data = await response.json();
        if (data.started) {
          window.location.assign(appUrl(data.room_url || joinUrl));
          return;
        }
        if (statusText) statusText.textContent = "Checked in and waiting";
      } catch (error) {
        if (statusText) statusText.textContent = "Still connected. Checking again...";
      }
    };

    pingWaitingRoom();
    poll();
    window.setInterval(pingWaitingRoom, 15000);
    window.setInterval(poll, 3500);
  }

  const room = document.querySelector("[data-consultation-room]");
  if (!room) return;

  const role = room.dataset.role;
  const statusUrl = appUrl(room.dataset.statusUrl || "");
  const signalsUrl = appUrl(room.dataset.signalsUrl || "");
  const iceServersUrl = appUrl(room.dataset.iceServersUrl || "");
  const startUrl = appUrl(room.dataset.startUrl || "");
  const exitUrl = appUrl(room.dataset.exitUrl || "");
  const startedOnLoad = room.dataset.started === "1";
  const localVideo = room.querySelector("[data-consultation-local]");
  const remoteVideo = room.querySelector("[data-consultation-remote]");
  const remoteEmpty = room.querySelector("[data-consultation-remote-empty]");
  const statusText = room.querySelector("[data-consultation-status]");
  const startButton = room.querySelector("[data-consultation-start]");
  const micButton = room.querySelector("[data-consultation-mic]");
  const cameraButton = room.querySelector("[data-consultation-camera]");
  const endForm = room.querySelector("[data-consultation-end-form]");
  const waitingCount = room.querySelector("[data-consultation-waiting-count]");
  const waitingList = room.querySelector("[data-consultation-waiting-list]");
  const waitingEmpty = room.querySelector("[data-consultation-waiting-empty]");
  const endOverlay = room.querySelector("[data-consultation-ended-overlay]");
  const endMessage = room.querySelector("[data-consultation-ended-message]");

  let peer = null;
  let localStream = null;
  let remoteMediaStream = null;
  let signalTimer = null;
  let statusTimer = null;
  let offerSent = false;
  let iceServerConfigPromise = null;
  let turnRelayConfigured = false;
  let restartingIce = false;
  let roomEnded = false;
  let endSoundContext = null;
  const pendingIceCandidates = [];
  const seenSignals = new Set();
  const fallbackIceServers = [{
    urls: [
      "stun:stun.l.google.com:19302",
      "stun:stun1.l.google.com:19302",
      "stun:stun2.l.google.com:19302",
    ],
  }];

  const setStatus = (message) => {
    if (statusText) statusText.textContent = message;
  };

  const sendSignal = async (type, payload) => {
    if (!signalsUrl) return;
    await postJson(signalsUrl, { type, payload: payload || {} });
  };

  const setIconButtonState = (button, enabled, labels) => {
    if (!button) return;
    const onIcon = button.querySelector("[data-icon-on]");
    const offIcon = button.querySelector("[data-icon-off]");
    if (onIcon) onIcon.hidden = !enabled;
    if (offIcon) offIcon.hidden = enabled;
    button.classList.toggle("is-off", !enabled);
    button.setAttribute("aria-label", enabled ? labels.on : labels.off);
    button.setAttribute("title", enabled ? labels.on : labels.off);
  };

  const syncMediaButtons = () => {
    const audioEnabled = !!localStream && localStream.getAudioTracks().some((track) => track.enabled);
    const videoEnabled = !!localStream && localStream.getVideoTracks().some((track) => track.enabled);
    setIconButtonState(micButton, audioEnabled, {
      on: "Mute microphone",
      off: "Unmute microphone",
    });
    setIconButtonState(cameraButton, videoEnabled, {
      on: "Turn camera off",
      off: "Turn camera on",
    });
  };

  const playVideo = (video) => {
    if (!video) return;
    const playPromise = video.play && video.play();
    if (playPromise && playPromise.catch) playPromise.catch(() => {});
  };

  const wait = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));

  const playEndSound = () => {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) return;
    try {
      endSoundContext = endSoundContext || new AudioContextClass();
      const context = endSoundContext;
      const playTone = () => {
        const start = context.currentTime + 0.02;
        const master = context.createGain();
        master.gain.setValueAtTime(0.0001, start);
        master.gain.exponentialRampToValueAtTime(0.07, start + 0.04);
        master.gain.exponentialRampToValueAtTime(0.0001, start + 0.9);
        master.connect(context.destination);

        [660, 523.25, 392].forEach((frequency, index) => {
          const toneStart = start + (index * 0.18);
          const oscillator = context.createOscillator();
          const gain = context.createGain();
          oscillator.type = "sine";
          oscillator.frequency.setValueAtTime(frequency, toneStart);
          gain.gain.setValueAtTime(0.0001, toneStart);
          gain.gain.exponentialRampToValueAtTime(0.55, toneStart + 0.03);
          gain.gain.exponentialRampToValueAtTime(0.0001, toneStart + 0.26);
          oscillator.connect(gain);
          gain.connect(master);
          oscillator.start(toneStart);
          oscillator.stop(toneStart + 0.28);
        });
      };

      if (context.state === "suspended") {
        context.resume().then(playTone).catch(() => {});
      } else {
        playTone();
      }
    } catch (error) {
      /* Audio cues are best effort because browsers can block autoplayed sounds. */
    }
  };

  const cleanupRoomMedia = () => {
    if (signalTimer) window.clearInterval(signalTimer);
    if (statusTimer) window.clearInterval(statusTimer);
    signalTimer = null;
    statusTimer = null;
    if (peer) {
      peer.onicecandidate = null;
      peer.ontrack = null;
      peer.onconnectionstatechange = null;
      peer.oniceconnectionstatechange = null;
      peer.close();
      peer = null;
    }
    if (localStream) {
      localStream.getTracks().forEach((track) => track.stop());
      localStream = null;
    }
    if (remoteMediaStream) {
      remoteMediaStream.getTracks().forEach((track) => track.stop());
      remoteMediaStream = null;
    }
    [localVideo, remoteVideo].forEach((video) => {
      if (!video) return;
      video.pause();
      video.srcObject = null;
    });
    if (remoteEmpty) remoteEmpty.classList.remove("hidden");
    if (micButton) micButton.disabled = true;
    if (cameraButton) cameraButton.disabled = true;
  };

  const showEndTransition = (message, redirectUrl) => {
    if (roomEnded) return;
    roomEnded = true;
    const finalMessage = message || "The live consultation has ended.";
    setStatus(finalMessage);
    if (endMessage) endMessage.textContent = finalMessage;
    room.classList.add("consult-room-ending");
    if (endOverlay) endOverlay.setAttribute("aria-hidden", "false");
    playEndSound();
    window.setTimeout(() => {
      room.classList.add("consult-room-ended");
    }, 80);
    window.setTimeout(cleanupRoomMedia, 420);
    if (redirectUrl) {
      window.setTimeout(() => {
        window.location.assign(appUrl(redirectUrl));
      }, 1800);
    }
  };

  const normalizeIceServers = (servers) => {
    if (!Array.isArray(servers)) return [];
    return servers.map((server) => {
      const rawUrls = server && server.urls;
      const urls = Array.isArray(rawUrls)
        ? rawUrls.filter(Boolean)
        : (rawUrls ? [rawUrls] : []);
      if (!urls.length) return null;
      const entry = { urls };
      if (server.username) entry.username = server.username;
      if (server.credential) entry.credential = server.credential;
      return entry;
    }).filter(Boolean);
  };

  const loadIceServerConfig = async () => {
    if (iceServerConfigPromise) return iceServerConfigPromise;
    iceServerConfigPromise = (async () => {
      if (!iceServersUrl) return { iceServers: fallbackIceServers };
      try {
        const response = await fetch(iceServersUrl, {
          headers: { Accept: "application/json" },
          cache: "no-store",
          credentials: "same-origin",
        });
        if (!response.ok) throw new Error("ICE config request failed");
        const data = await response.json();
        const iceServers = normalizeIceServers(data.iceServers);
        turnRelayConfigured = !!data.turnConfigured;
        const config = {
          iceServers: iceServers.length ? iceServers : fallbackIceServers,
        };
        if (data.iceTransportPolicy === "relay") {
          config.iceTransportPolicy = "relay";
        }
        return config;
      } catch (error) {
        turnRelayConfigured = false;
        return { iceServers: fallbackIceServers };
      }
    })();
    return iceServerConfigPromise;
  };

  const ensureMedia = async () => {
    if (localStream) return localStream;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      setStatus("This browser cannot access camera and microphone devices.");
      if (micButton) micButton.disabled = true;
      if (cameraButton) cameraButton.disabled = true;
      return null;
    }
    try {
      localStream = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 1280 }, height: { ideal: 720 } },
        audio: true,
      });
      if (localVideo) {
        localVideo.srcObject = localStream;
        playVideo(localVideo);
      }
      if (micButton) micButton.disabled = false;
      if (cameraButton) cameraButton.disabled = false;
      syncMediaButtons();
      return localStream;
    } catch (error) {
      setStatus("Camera or microphone permission was blocked. You can still stay in the room.");
      if (micButton) micButton.disabled = true;
      if (cameraButton) cameraButton.disabled = true;
      return null;
    }
  };

  const connectionFailureMessage = () => {
    if (turnRelayConfigured) {
      return "Connection dropped. Rebuilding the video link...";
    }
    if (role === "doctor") {
      return "Connection failed. Add TURN relay settings on the hosted server, then retry.";
    }
    return "Connection could not reach the doctor. Please stay here while it retries.";
  };

  const flushPendingIceCandidates = async (pc) => {
    if (!pc.remoteDescription || !pendingIceCandidates.length) return;
    while (pendingIceCandidates.length) {
      const candidate = pendingIceCandidates.shift();
      try {
        await pc.addIceCandidate(candidate);
      } catch (error) {
        /* Stale candidates can be ignored after a reconnect attempt. */
      }
    }
  };

  const requestIceRestart = () => {
    if (restartingIce) return;
    restartingIce = true;
    window.setTimeout(() => {
      restartingIce = false;
    }, 7000);

    const restart = async () => {
      if (role === "doctor") {
        await createOffer({ restart: true });
      } else {
        await sendSignal("ready", {
          role,
          reason: "ice_failed",
          at: new Date().toISOString(),
        });
      }
    };
    restart().catch(() => {});
  };

  const updatePeerConnectionStatus = (state) => {
    if (roomEnded) return;
    if (state === "checking" || state === "connecting") {
      setStatus("Connecting securely...");
    } else if (state === "connected" || state === "completed") {
      setStatus("Connected securely.");
    } else if (state === "disconnected") {
      setStatus("Connection interrupted. Trying to reconnect...");
    } else if (state === "failed") {
      setStatus(connectionFailureMessage());
      requestIceRestart();
    } else if (state === "closed") {
      setStatus("The video session has ended.");
    }
  };

  const ensurePeer = async () => {
    if (peer) return peer;
    await ensureMedia();
    const peerConfig = await loadIceServerConfig();
    peer = new RTCPeerConnection({
      bundlePolicy: "max-bundle",
      iceCandidatePoolSize: 4,
      ...peerConfig,
    });

    if (localStream) {
      localStream.getTracks().forEach((track) => peer.addTrack(track, localStream));
    } else {
      try {
        peer.addTransceiver("video", { direction: "recvonly" });
        peer.addTransceiver("audio", { direction: "recvonly" });
      } catch (error) {
        /* Older browsers may not support explicit recvonly transceivers. */
      }
    }

    peer.onicecandidate = (event) => {
      if (event.candidate) sendSignal("ice", event.candidate).catch(() => {});
    };

    peer.ontrack = (event) => {
      const stream = event.streams && event.streams[0] ? event.streams[0] : new MediaStream([event.track]);
      remoteMediaStream = stream;
      if (remoteVideo) {
        remoteVideo.srcObject = stream;
        playVideo(remoteVideo);
      }
      if (remoteEmpty) remoteEmpty.classList.add("hidden");
    };

    peer.onconnectionstatechange = () => {
      if (!peer) return;
      updatePeerConnectionStatus(peer.connectionState);
    };

    peer.oniceconnectionstatechange = () => {
      if (!peer) return;
      updatePeerConnectionStatus(peer.iceConnectionState);
    };

    return peer;
  };

  const createOffer = async (options = {}) => {
    const restart = options.restart === true;
    if (offerSent && !restart) return;
    const pc = await ensurePeer();
    if (pc.signalingState !== "stable") return;
    const offer = restart ? await pc.createOffer({ iceRestart: true }) : await pc.createOffer();
    await pc.setLocalDescription(offer);
    await sendSignal("offer", pc.localDescription);
    offerSent = true;
    setStatus(restart ? "Rebuilding the secure video link..." : "Session open. Waiting for the patient to connect...");
  };

  const handleSignal = async (signal) => {
    if (!signal || seenSignals.has(signal.id) || roomEnded) return;
    seenSignals.add(signal.id);
    const payload = signal.payload || {};

    if (signal.type === "leave") {
      if (payload.reason === "ended_by_doctor") {
        showEndTransition("The live consultation has ended.", exitUrl);
      } else {
        setStatus("The other participant left the session.");
      }
      return;
    }

    const pc = await ensurePeer();

    if (signal.type === "offer") {
      if (pc.signalingState !== "stable") return;
      await pc.setRemoteDescription(new RTCSessionDescription(payload));
      await flushPendingIceCandidates(pc);
      const answer = await pc.createAnswer();
      await pc.setLocalDescription(answer);
      await sendSignal("answer", pc.localDescription);
      setStatus("Joining the secure video session...");
    } else if (signal.type === "answer") {
      if (pc.signalingState === "have-local-offer") {
        await pc.setRemoteDescription(new RTCSessionDescription(payload));
        await flushPendingIceCandidates(pc);
      }
      setStatus("Connecting securely...");
    } else if (signal.type === "ice") {
      const candidate = new RTCIceCandidate(payload);
      if (!pc.remoteDescription || !pc.remoteDescription.type) {
        pendingIceCandidates.push(candidate);
        return;
      }
      try {
        await pc.addIceCandidate(candidate);
      } catch (error) {
        /* Candidate timing can race session descriptions; the next one usually succeeds. */
      }
    } else if (signal.type === "ready" && role === "doctor") {
      await createOffer({ restart: payload.reason === "ice_failed" });
    }
  };

  const pollSignals = async () => {
    if (roomEnded || !signalsUrl) return;
    try {
      const response = await fetch(signalsUrl, {
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) return;
      const data = await response.json();
      for (const signal of data.signals || []) {
        await handleSignal(signal);
      }
    } catch (error) {
      setStatus("Reconnecting to the secure room...");
    }
  };

  const renderWaitingParticipants = (participants) => {
    if (!waitingCount || !waitingList || !waitingEmpty) return;
    const rows = Array.isArray(participants) ? participants : [];
    const count = rows.length;
    waitingCount.textContent = count === 1 ? "1 patient waiting" : `${count} patients waiting`;
    waitingList.replaceChildren();
    rows.forEach((participant) => {
      const item = document.createElement("li");
      item.textContent = `${participant.name || "Patient"} is checked in`;
      waitingList.appendChild(item);
    });
    waitingList.hidden = count === 0;
    waitingEmpty.hidden = count > 0;
  };

  const fetchRoomStatus = async () => {
    if (!statusUrl || roomEnded) return;
    const response = await fetch(statusUrl, {
      credentials: "same-origin",
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) return;
    const data = await response.json();
    if (data.status === "completed" || data.status === "cancelled") {
      const message = data.status === "cancelled"
        ? "The live consultation was cancelled."
        : "The live consultation has ended.";
      showEndTransition(message, exitUrl);
      return;
    }
    if (role === "doctor") {
      renderWaitingParticipants(data.waiting_participants || []);
      if (data.started && startButton) startButton.classList.add("hidden");
    }
    if (role === "patient" && data.started && !peer) {
      await beginRoom(false);
    }
  };

  const watchStatus = () => {
    if (!statusUrl || statusTimer) return;
    fetchRoomStatus().catch(() => {});
    statusTimer = window.setInterval(() => {
      fetchRoomStatus().catch(() => {});
    }, 3000);
  };

  const beginRoom = async (makeOffer) => {
    await ensurePeer();
    await sendSignal("ready", { role });
    if (!signalTimer) signalTimer = window.setInterval(pollSignals, 1400);
    await pollSignals();
    if (makeOffer) await createOffer();
    setStatus(makeOffer ? "Session open." : "Joining secure session...");
  };

  const startDoctorSession = async () => {
    if (!startUrl) return;
    if (startButton) {
      startButton.disabled = true;
      startButton.textContent = "Starting...";
    }
    setStatus("Opening secure room...");
    try {
      const response = await fetch(startUrl, {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "text/html,application/json" },
      });
      if (!response.ok) throw new Error("Start failed");
      await beginRoom(true);
      if (startButton) startButton.classList.add("hidden");
      fetchRoomStatus().catch(() => {});
    } catch (error) {
      setStatus("Could not start the session. Please try again.");
      if (startButton) {
        startButton.disabled = false;
        startButton.textContent = "Start session";
      }
    }
  };

  micButton?.addEventListener("click", async () => {
    if (!localStream) await ensureMedia();
    if (!localStream) return;
    const enabled = localStream.getAudioTracks().some((track) => track.enabled);
    localStream.getAudioTracks().forEach((track) => { track.enabled = !enabled; });
    syncMediaButtons();
  });

  cameraButton?.addEventListener("click", async () => {
    if (!localStream) await ensureMedia();
    if (!localStream) return;
    const enabled = localStream.getVideoTracks().some((track) => track.enabled);
    localStream.getVideoTracks().forEach((track) => { track.enabled = !enabled; });
    syncMediaButtons();
  });

  endForm?.addEventListener("submit", async (event) => {
    if (endForm.dataset.submitting === "1") return;
    event.preventDefault();
    endForm.dataset.submitting = "1";
    const submitButton = endForm.querySelector("button[type='submit']");
    if (submitButton) {
      submitButton.disabled = true;
      submitButton.textContent = "Ending...";
    }
    setStatus("Ending consultation...");
    try {
      await sendSignal("leave", {
        role,
        reason: "ended_by_doctor",
        at: new Date().toISOString(),
      });
    } catch (error) {
      /* Leaving is best effort once the session is ending. */
    }
    showEndTransition("Session ended. Returning to consultations...", exitUrl);
    try {
      const response = await fetch(endForm.action, {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "text/html,application/xhtml+xml" },
      });
      const targetUrl = response.url || exitUrl || endForm.action;
      await wait(900);
      window.location.assign(appUrl(targetUrl));
    } catch (error) {
      HTMLFormElement.prototype.submit.call(endForm);
    }
  });

  window.addEventListener("beforeunload", () => {
    if (!roomEnded && peer) {
      sendSignal("leave", { role }).catch(() => {});
      peer.close();
    }
  });

  startButton?.addEventListener("click", startDoctorSession);
  watchStatus();

  if (startedOnLoad) {
    beginRoom(role === "doctor").catch(() => {
      setStatus("Could not prepare the secure video session.");
    });
  }
})();
