(function () {
  const postJson = async (url, body) => {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error("Request failed");
    return response;
  };

  const waiting = document.querySelector("[data-consultation-waiting]");
  if (waiting) {
    const statusUrl = waiting.dataset.statusUrl;
    const signalsUrl = waiting.dataset.signalsUrl;
    const joinUrl = waiting.dataset.joinUrl;
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
        const response = await fetch(statusUrl, { headers: { Accept: "application/json" } });
        if (!response.ok) return;
        const data = await response.json();
        if (data.started) {
          window.location.assign(data.room_url || joinUrl);
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
  const statusUrl = room.dataset.statusUrl;
  const signalsUrl = room.dataset.signalsUrl;
  const iceServersUrl = room.dataset.iceServersUrl;
  const startUrl = room.dataset.startUrl;
  const recordingUrl = room.dataset.recordingUrl;
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

  let peer = null;
  let localStream = null;
  let remoteMediaStream = null;
  let signalTimer = null;
  let statusTimer = null;
  let offerSent = false;
  let iceServerConfigPromise = null;
  let turnRelayConfigured = false;
  let restartingIce = false;
  const pendingIceCandidates = [];
  const seenSignals = new Set();
  const fallbackIceServers = [{ urls: ["stun:stun.l.google.com:19302"] }];

  let mediaRecorder = null;
  let recordingId = "";
  let recordingMimeType = "";
  let recordingCanvas = null;
  let recordingContext = null;
  let recordingCanvasStream = null;
  let recordingDrawTimer = null;
  let recordingAudioContext = null;
  let recordingAudioDestination = null;
  let recordingUploadQueue = Promise.resolve();
  let recordingUploadFailed = false;
  let recordingChunkSequence = 0;
  let recordingStarted = false;
  let recordingConnectedStreams = new WeakSet();
  const recordingAudioSources = [];

  const setStatus = (message) => {
    if (statusText) statusText.textContent = message;
  };

  const sendSignal = async (type, payload) => {
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

  const hasLiveVideo = (stream) => (
    !!stream && stream.getVideoTracks().some((track) => track.readyState === "live" && track.enabled)
  );

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
      addAudioStreamToRecording(localStream);
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
      return "Connection failed. Add TURN relay settings on PythonAnywhere, then retry.";
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
    peer = new RTCPeerConnection(await loadIceServerConfig());

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
      addAudioStreamToRecording(stream);
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
    if (!signal || seenSignals.has(signal.id)) return;
    seenSignals.add(signal.id);
    const pc = await ensurePeer();
    const payload = signal.payload || {};

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
    if (!statusUrl) return;
    const response = await fetch(statusUrl, { headers: { Accept: "application/json" } });
    if (!response.ok) return;
    const data = await response.json();
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

  const drawPlaceholder = (ctx, x, y, width, height, label) => {
    ctx.fillStyle = "#020617";
    ctx.fillRect(x, y, width, height);
    ctx.fillStyle = "#0f172a";
    ctx.fillRect(x + 1, y + 1, Math.max(0, width - 2), Math.max(0, height - 2));
    ctx.fillStyle = "#94a3b8";
    ctx.font = `${Math.max(14, Math.round(width / 32))}px sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(label, x + width / 2, y + height / 2);
  };

  const drawVideoCover = (ctx, video, x, y, width, height) => {
    if (!video || video.readyState < 2 || !video.videoWidth || !video.videoHeight) return false;
    const sourceRatio = video.videoWidth / video.videoHeight;
    const targetRatio = width / height;
    let sx = 0;
    let sy = 0;
    let sw = video.videoWidth;
    let sh = video.videoHeight;
    if (sourceRatio > targetRatio) {
      sw = video.videoHeight * targetRatio;
      sx = (video.videoWidth - sw) / 2;
    } else {
      sh = video.videoWidth / targetRatio;
      sy = (video.videoHeight - sh) / 2;
    }
    ctx.drawImage(video, sx, sy, sw, sh, x, y, width, height);
    return true;
  };

  const drawRecordingFrame = () => {
    if (!recordingContext || !recordingCanvas) return;
    const ctx = recordingContext;
    const width = recordingCanvas.width;
    const height = recordingCanvas.height;
    const remoteDrawn = drawVideoCover(ctx, remoteVideo, 0, 0, width, height);
    if (!remoteDrawn) {
      drawPlaceholder(ctx, 0, 0, width, height, remoteMediaStream ? "Camera off" : "Waiting for patient");
    }

    const inset = Math.round(width * 0.025);
    const pipWidth = Math.round(width * 0.28);
    const pipHeight = Math.round(pipWidth * 9 / 16);
    const pipX = width - pipWidth - inset;
    const pipY = height - pipHeight - inset;
    ctx.fillStyle = "rgba(2, 6, 23, 0.82)";
    ctx.fillRect(pipX - 4, pipY - 4, pipWidth + 8, pipHeight + 8);
    if (hasLiveVideo(localStream)) {
      drawVideoCover(ctx, localVideo, pipX, pipY, pipWidth, pipHeight);
    } else {
      drawPlaceholder(ctx, pipX, pipY, pipWidth, pipHeight, "You");
    }
    ctx.fillStyle = "rgba(2, 6, 23, 0.72)";
    ctx.fillRect(pipX, pipY + pipHeight - 30, 58, 30);
    ctx.fillStyle = "#e2e8f0";
    ctx.font = "14px sans-serif";
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText("You", pipX + 12, pipY + pipHeight - 15);

    recordingDrawTimer = window.setTimeout(drawRecordingFrame, 1000 / 15);
  };

  const chooseRecordingMime = () => {
    if (!window.MediaRecorder || !MediaRecorder.isTypeSupported) return "";
    return [
      "video/webm;codecs=vp9,opus",
      "video/webm;codecs=vp8,opus",
      "video/webm",
    ].find((mime) => MediaRecorder.isTypeSupported(mime)) || "";
  };

  function addAudioStreamToRecording(stream) {
    if (!recordingAudioContext || !recordingAudioDestination || !stream) return;
    if (recordingConnectedStreams.has(stream)) return;
    if (!stream.getAudioTracks().some((track) => track.readyState === "live")) return;
    try {
      const source = recordingAudioContext.createMediaStreamSource(stream);
      source.connect(recordingAudioDestination);
      recordingAudioSources.push(source);
      recordingConnectedStreams.add(stream);
    } catch (error) {
      /* Audio capture is best effort; video replay should still be saved. */
    }
  }

  const queueRecordingChunk = (blob) => {
    if (!recordingUrl || !recordingId || !blob || blob.size <= 0) return;
    const sequence = recordingChunkSequence;
    recordingChunkSequence += 1;
    recordingUploadQueue = recordingUploadQueue
      .then(async () => {
        if (recordingUploadFailed) return;
        const form = new FormData();
        form.append("recording_id", recordingId);
        form.append("sequence", String(sequence));
        form.append("mime", recordingMimeType || blob.type || "video/webm");
        form.append("extension", ".webm");
        form.append("chunk", blob, `chunk-${String(sequence).padStart(5, "0")}.webm`);
        const response = await fetch(recordingUrl, {
          method: "POST",
          headers: { Accept: "application/json" },
          body: form,
        });
        if (!response.ok) throw new Error("Chunk upload failed");
      })
      .catch(() => {
        recordingUploadFailed = true;
      });
  };

  const finalizeRecordingUpload = async () => {
    await recordingUploadQueue;
    if (recordingUploadFailed) throw new Error("Recording upload failed");
    if (!recordingChunkSequence) throw new Error("No recording data captured");
    const form = new FormData();
    form.append("complete", "1");
    form.append("recording_id", recordingId);
    form.append("mime", recordingMimeType || "video/webm");
    form.append("extension", ".webm");
    const response = await fetch(recordingUrl, {
      method: "POST",
      headers: { Accept: "application/json" },
      body: form,
    });
    if (!response.ok) throw new Error("Recording finalize failed");
    return response.json();
  };

  const startRecording = async () => {
    if (role !== "doctor" || !recordingUrl || recordingStarted || !window.MediaRecorder) return;
    recordingStarted = true;
    recordingCanvas = document.createElement("canvas");
    recordingCanvas.width = 960;
    recordingCanvas.height = 540;
    recordingContext = recordingCanvas.getContext("2d");
    if (!recordingContext || !recordingCanvas.captureStream) return;

    recordingCanvasStream = recordingCanvas.captureStream(15);
    const mixedStream = new MediaStream();
    recordingCanvasStream.getVideoTracks().forEach((track) => mixedStream.addTrack(track));

    try {
      const AudioContextClass = window.AudioContext || window.webkitAudioContext;
      if (AudioContextClass) {
        recordingAudioContext = new AudioContextClass();
        recordingAudioDestination = recordingAudioContext.createMediaStreamDestination();
        if (recordingAudioContext.state === "suspended") {
          await recordingAudioContext.resume().catch(() => {});
        }
        addAudioStreamToRecording(localStream);
        addAudioStreamToRecording(remoteMediaStream);
        recordingAudioDestination.stream.getAudioTracks().forEach((track) => mixedStream.addTrack(track));
      }
    } catch (error) {
      recordingAudioContext = null;
      recordingAudioDestination = null;
    }

    recordingMimeType = chooseRecordingMime();
    recordingId = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const recorderOptions = {
      videoBitsPerSecond: 750000,
      audioBitsPerSecond: 48000,
    };
    if (recordingMimeType) recorderOptions.mimeType = recordingMimeType;

    try {
      mediaRecorder = new MediaRecorder(mixedStream, recorderOptions);
    } catch (error) {
      try {
        mediaRecorder = new MediaRecorder(mixedStream);
      } catch (fallbackError) {
        if (recordingCanvasStream) recordingCanvasStream.getTracks().forEach((track) => track.stop());
        setStatus("This browser can run the video session, but cannot record it for replay.");
        return;
      }
    }
    mediaRecorder.addEventListener("dataavailable", (event) => {
      if (event.data && event.data.size > 0) queueRecordingChunk(event.data);
    });
    mediaRecorder.start(10000);
    drawRecordingFrame();
  };

  const stopRecording = async () => {
    if (!mediaRecorder || mediaRecorder.state === "inactive") return;
    setStatus("Saving session video before ending...");
    const stopped = new Promise((resolve) => {
      mediaRecorder.addEventListener("stop", resolve, { once: true });
    });
    try {
      mediaRecorder.requestData();
    } catch (error) {
      /* Some browsers do not allow requestData right before stop. */
    }
    mediaRecorder.stop();
    await stopped;
    if (recordingDrawTimer) window.clearTimeout(recordingDrawTimer);
    if (recordingCanvasStream) recordingCanvasStream.getTracks().forEach((track) => track.stop());
    if (recordingAudioContext) await recordingAudioContext.close().catch(() => {});
    await finalizeRecordingUpload();
    setStatus("Session video saved. Ending consultation...");
  };

  const beginRoom = async (makeOffer) => {
    await ensurePeer();
    if (role === "doctor") await startRecording();
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
      const response = await fetch(startUrl, { method: "POST", headers: { Accept: "text/html,application/json" } });
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
    if (role !== "doctor" || !mediaRecorder) return;
    event.preventDefault();
    endForm.dataset.submitting = "1";
    const submitButton = endForm.querySelector("button[type='submit']");
    if (submitButton) {
      submitButton.disabled = true;
      submitButton.textContent = "Saving video...";
    }
    try {
      await stopRecording();
    } catch (error) {
      setStatus("The video could not be saved. Ending the consultation...");
      await new Promise((resolve) => window.setTimeout(resolve, 800));
    }
    try {
      await sendSignal("leave", { role });
    } catch (error) {
      /* Leaving is best effort once the session is ending. */
    }
    if (peer) peer.close();
    HTMLFormElement.prototype.submit.call(endForm);
  });

  window.addEventListener("beforeunload", () => {
    if (peer) {
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
