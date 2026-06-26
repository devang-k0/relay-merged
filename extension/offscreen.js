/**
 * offscreen.js
 *
 * Runs inside the hidden offscreen document. Responsibilities:
 *  - Capture the meeting tab's audio via the streamId handed to us
 *    from background.js (tabCapture.getMediaStreamId).
 *  - Capture the user's microphone via getUserMedia.
 *  - Mix both into a single stereo track using the Web Audio API.
 *  - Encode the mixed stream with MediaRecorder (Opus/WebM, 128kbps).
 *  - On stop, create a blob: URL for the finished recording (offscreen
 *    documents are a supported context for URL.createObjectURL via the
 *    "BLOBS" offscreen reason) and hand that URL back to background.js,
 *    which performs the actual chrome.downloads.download call — saveAs
 *    downloads must be initiated from the background/service-worker
 *    context. A blob: URL avoids the ~2MB practical size ceiling that
 *    Chrome enforces on data: URLs, so multi-minute and multi-hour
 *    recordings still save reliably.
 */

let mediaRecorder = null;
let recordedChunks = [];
let audioContext = null;
let tabStream = null;
let micStream = null;

/** Build a single mixed stream from the tab-capture stream and the mic stream. */
async function buildMixedStream(tabMediaStreamId) {
  audioContext = new AudioContext();

  // --- Tab audio (remote participants) ---
  tabStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      mandatory: {
        chromeMediaSource: "tab",
        chromeMediaSourceId: tabMediaStreamId,
      },
    },
    video: false,
  });

  // IMPORTANT: routing the captured tab stream through an audio element
  // (muted-to-speakers via destination, not muted overall) keeps the
  // meeting's own audio playing normally for the user — tabCapture by
  // default would otherwise silence the tab.
  const tabSource = audioContext.createMediaStreamSource(tabStream);

  // If the tab's audio track ends unexpectedly (navigation, capture revoked),
  // make sure we don't keep recording against a dead stream indefinitely.
  tabStream.getAudioTracks().forEach((track) => {
    track.onended = () => {
      if (mediaRecorder && mediaRecorder.state === "recording") {
        mediaRecorder.stop();
      }
    };
  });

  // --- Microphone audio (the user's own voice) ---
  micStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
    },
    video: false,
  });
  const micSource = audioContext.createMediaStreamSource(micStream);

  // --- Mix both sources into one destination node ---
  const destination = audioContext.createMediaStreamDestination();
  // Force a consistent stereo channel count on the destination regardless
  // of how many channels the tab or mic streams individually report —
  // a channel-count mismatch between sources feeding one destination is
  // a known cause of corrupt/unplayable WebM output from MediaRecorder.
  destination.channelCount = 2;
  tabSource.connect(destination);
  micSource.connect(destination);

  // Also route the tab audio to actual speakers so the meeting isn't
  // silenced for the user while we record it.
  tabSource.connect(audioContext.destination);

  return destination.stream;
}

function cleanupStreams() {
  if (tabStream) {
    tabStream.getTracks().forEach((t) => t.stop());
    tabStream = null;
  }
  if (micStream) {
    micStream.getTracks().forEach((t) => t.stop());
    micStream = null;
  }
  if (audioContext) {
    audioContext.close().catch(() => {});
    audioContext = null;
  }
}

async function handleStartRecording(streamId) {
  try {
    const mixedStream = await buildMixedStream(streamId);

    recordedChunks = [];

    // Pick the best supported mimeType instead of assuming opus/webm is
    // always available — if it silently isn't, MediaRecorder can still
    // "work" but produce chunks that don't form a valid container when
    // concatenated, which is exactly what causes unplayable output files.
    const preferredMimeTypes = [
      "audio/webm;codecs=opus",
      "audio/webm",
    ];
    const mimeType = preferredMimeTypes.find((type) =>
      MediaRecorder.isTypeSupported(type)
    );
    if (!mimeType) {
      cleanupStreams();
      return { ok: false, error: "This browser doesn't support WebM/Opus recording." };
    }

    mediaRecorder = new MediaRecorder(mixedStream, {
      mimeType,
      audioBitsPerSecond: 128000,
    });

    mediaRecorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0) {
        recordedChunks.push(event.data);
      }
    };

    // If the recorder errors mid-session (track ended unexpectedly, system
    // audio suspend, etc.), onstop may never fire on its own. Force a stop
    // so whatever was captured so far still gets flushed instead of hanging.
    mediaRecorder.onerror = (event) => {
      console.error("Relay: MediaRecorder error", event.error);
      if (mediaRecorder && mediaRecorder.state !== "inactive") {
        try {
          mediaRecorder.stop();
        } catch (e) {
          // already stopping/stopped — nothing more to do
        }
      }
    };

    mediaRecorder.start(1000); // gather chunks every second so partial data is safe
    return { ok: true };
  } catch (err) {
    cleanupStreams();
    // Surface a friendly, specific reason where we can tell what failed.
    let message = "Could not start recording.";
    if (err && err.name === "NotAllowedError") {
      message = "Microphone permission was denied.";
    } else if (err && err.name === "NotFoundError") {
      message = "No microphone was found.";
    } else if (err && err.message) {
      message = err.message;
    }
    return { ok: false, error: message };
  }
}

function handleStopRecording() {
  return new Promise((resolve) => {
    if (!mediaRecorder || mediaRecorder.state === "inactive") {
      cleanupStreams();
      resolve({ ok: false, error: "No active recording to stop." });
      return;
    }

    mediaRecorder.onstop = async () => {
      try {
        if (recordedChunks.length === 0) {
          cleanupStreams();
          resolve({
            ok: false,
            error: "No audio was captured — the recording was empty.",
          });
          return;
        }

        const blob = new Blob(recordedChunks, { type: "audio/webm" });
        cleanupStreams();

        if (blob.size === 0) {
          resolve({
            ok: false,
            error: "The recording file came out empty.",
          });
          return;
        }

        // Use a blob: URL rather than a base64 data: URL. Chrome enforces
        // a practical ~2MB ceiling on data: URLs, which silently breaks
        // saving for any recording longer than roughly a minute and a
        // half. blob: URLs created here are scoped to the extension's
        // own origin (chrome-extension://<id>/...), so background.js can
        // resolve the same URL and pass it straight to
        // chrome.downloads.download.
        const blobUrl = URL.createObjectURL(blob);
        resolve({ ok: true, blobUrl });
      } catch (e) {
        cleanupStreams();
        resolve({ ok: false, error: "Failed to finalize the recording file." });
      }
    };

    // stop() always flushes one final ondataavailable with whatever's
    // buffered before onstop fires — no separate requestData() call needed,
    // and calling both back-to-back can race in some Chrome versions.
    mediaRecorder.stop();
  });
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.target !== "offscreen") return;

  (async () => {
    switch (message.type) {
      case "start-recording": {
        const result = await handleStartRecording(message.streamId);
        sendResponse(result);
        break;
      }
      case "stop-recording": {
        const result = await handleStopRecording();
        sendResponse(result);
        break;
      }
      default:
        sendResponse({ ok: false, error: "Unknown offscreen message type." });
    }
  })();

  return true;
});
