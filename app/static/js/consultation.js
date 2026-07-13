(function () {
  const waiting = document.querySelector("[data-consultation-waiting]");
  if (waiting) {
    const statusUrl = waiting.dataset.statusUrl;
    const joinUrl = waiting.dataset.joinUrl;
    const statusText = waiting.querySelector("[data-consultation-waiting-status]");

    const poll = async () => {
      try {
        const response = await fetch(statusUrl, { headers: { Accept: "application/json" } });
        if (!response.ok) return;
        const data = await response.json();
        if (data.started) {
          window.location.assign(data.room_url || joinUrl);
          return;
        }
        if (statusText) statusText.textContent = "Waiting for doctor to start";
      } catch (error) {
        if (statusText) statusText.textContent = "Still waiting. Reconnecting status check...";
      }
    };

    poll();
    window.setInterval(poll, 3500);
  }

  const room = document.querySelector("[data-consultation-room]");
  if (!room) return;

  const role = room.dataset.role;
  const statusUrl = room.dataset.statusUrl;
  const signalsUrl = room.dataset.signalsUrl;
  const startUrl = room.dataset.startUrl;
  const startedOnLoad = room.dataset.started === "1";
  const localVideo = room.querySelector("[data-consultation-local]");
  const remoteVideo = room.querySelector("[data-consultation-remote]");
  const remoteEmpty = room.querySelector("[data-consultation-remote-empty]");
  const statusText = room.querySelector("[data-consultation-status]");
  const startButton = room.querySelector("[data-consultation-start]");
  const micButton = room.querySelector("[data-consultation-mic]");
  const cameraButton = room.querySelector("[data-consultation-camera]");

  let peer = null;
  let localStream = null;
  let signalTimer = null;
  let statusTimer = null;
  let offerSent = false;
  const seenSignals = new Set();

  const setStatus = (message) => {
    if (statusText) statusText.textContent = message;
  };

  const postJson = async (url, body) => {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error("Request failed");
    return response;
  };

  const sendSignal = async (type, payload) => {
    await postJson(signalsUrl, { type, payload: payload || {} });
  };

  const ensureMedia = async () => {
    if (localStream) return localStream;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      setStatus("This browser cannot access camera and microphone devices.");
      return null;
    }
    try {
      localStream = await navigator.mediaDevices.getUserMedia({ video: true, audio: true });
      if (localVideo) localVideo.srcObject = localStream;
      return localStream;
    } catch (error) {
      setStatus("Camera or microphone permission was blocked. You can still stay in the room.");
      return null;
    }
  };

  const ensurePeer = async () => {
    if (peer) return peer;
    await ensureMedia();
    peer = new RTCPeerConnection({
      iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
    });

    if (localStream) {
      localStream.getTracks().forEach((track) => peer.addTrack(track, localStream));
    }

    peer.onicecandidate = (event) => {
      if (event.candidate) sendSignal("ice", event.candidate).catch(() => {});
    };

    peer.ontrack = (event) => {
      if (remoteVideo && event.streams && event.streams[0]) {
        remoteVideo.srcObject = event.streams[0];
        if (remoteEmpty) remoteEmpty.classList.add("hidden");
      }
    };

    peer.onconnectionstatechange = () => {
      if (!peer) return;
      const state = peer.connectionState;
      if (state === "connected") setStatus("Connected securely.");
      if (state === "disconnected") setStatus("Connection interrupted. Trying to reconnect...");
      if (state === "failed") setStatus("Connection failed. Refresh the room to retry.");
    };

    return peer;
  };

  const createOffer = async () => {
    if (offerSent) return;
    const pc = await ensurePeer();
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    await sendSignal("offer", pc.localDescription);
    offerSent = true;
    setStatus("Session open. Waiting for the patient to connect...");
  };

  const handleSignal = async (signal) => {
    if (!signal || seenSignals.has(signal.id)) return;
    seenSignals.add(signal.id);
    const pc = await ensurePeer();
    const payload = signal.payload || {};

    if (signal.type === "offer") {
      await pc.setRemoteDescription(new RTCSessionDescription(payload));
      const answer = await pc.createAnswer();
      await pc.setLocalDescription(answer);
      await sendSignal("answer", pc.localDescription);
      setStatus("Joining the secure video session...");
    } else if (signal.type === "answer") {
      if (!pc.currentRemoteDescription) {
        await pc.setRemoteDescription(new RTCSessionDescription(payload));
      }
      setStatus("Connecting securely...");
    } else if (signal.type === "ice") {
      try {
        await pc.addIceCandidate(new RTCIceCandidate(payload));
      } catch (error) {
        /* Candidate may arrive before descriptions settle; the next one usually succeeds. */
      }
    } else if (signal.type === "ready" && role === "doctor" && pc.signalingState === "stable") {
      await createOffer();
    } else if (signal.type === "leave") {
      setStatus("The other participant left the session.");
    }
  };

  const pollSignals = async () => {
    try {
      const response = await fetch(signalsUrl, { headers: { Accept: "application/json" } });
      if (!response.ok) return;
      const data = await response.json();
      for (const signal of data.signals || []) {
        await handleSignal(signal);
      }
    } catch (error) {
      setStatus("Reconnecting to the secure room...");
    }
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
      await fetch(startUrl, { method: "POST", headers: { Accept: "text/html,application/json" } });
      await beginRoom(true);
      if (startButton) startButton.classList.add("hidden");
    } catch (error) {
      setStatus("Could not start the session. Please try again.");
      if (startButton) {
        startButton.disabled = false;
        startButton.textContent = "Start session";
      }
    }
  };

  const watchStatus = () => {
    if (!statusUrl || statusTimer) return;
    statusTimer = window.setInterval(async () => {
      try {
        const response = await fetch(statusUrl, { headers: { Accept: "application/json" } });
        if (!response.ok) return;
        const data = await response.json();
        if (role === "patient" && data.started && !peer) {
          await beginRoom(false);
        }
      } catch (error) {
        /* Status polling is best effort once the room is loaded. */
      }
    }, 3000);
  };

  micButton?.addEventListener("click", () => {
    if (!localStream) return;
    const enabled = localStream.getAudioTracks().some((track) => track.enabled);
    localStream.getAudioTracks().forEach((track) => { track.enabled = !enabled; });
    micButton.textContent = enabled ? "Unmute mic" : "Mute mic";
  });

  cameraButton?.addEventListener("click", () => {
    if (!localStream) return;
    const enabled = localStream.getVideoTracks().some((track) => track.enabled);
    localStream.getVideoTracks().forEach((track) => { track.enabled = !enabled; });
    cameraButton.textContent = enabled ? "Camera on" : "Camera off";
  });

  window.addEventListener("beforeunload", () => {
    if (peer) {
      sendSignal("leave", { role }).catch(() => {});
      peer.close();
    }
  });

  startButton?.addEventListener("click", startDoctorSession);

  if (startedOnLoad) {
    beginRoom(role === "doctor").catch(() => {
      setStatus("Could not prepare the secure video session.");
    });
  } else if (role === "patient") {
    watchStatus();
  }
})();
