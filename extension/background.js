/**
 * background.js (Manifest V3 service worker)
 *
 * Responsibilities:
 *  - Track recording state (idle / recording) and which tab is being recorded.
 *  - Own the lifecycle of the offscreen document, which does the actual
 *    audio capture + mixing + MediaRecorder work (service workers can't
 *    touch getUserMedia / tabCapture streams directly).
 *  - Inject a visible on-page "Relay is recording" banner into the meeting
 *    tab so every participant sharing that tab/screen can see recording
 *    is happening — this is a deliberate, non-optional part of the design.
 *  - Relay messages between the popup and the offscreen document.
 *  - Detect if the recorded tab is closed mid-recording and stop gracefully.
 */

importScripts("config.js");

const OFFSCREEN_PATH = "offscreen.html";

// In-memory state (service worker can be evicted, so we also mirror
// critical bits to chrome.storage.session for popup reads after eviction).
let state = {
  status: "idle", // "idle" | "recording" | "stopping"
  tabId: null,
  startedAt: null, // epoch ms
  lastError: null,
};

async function persistState() {
  await chrome.storage.session.set({ relayState: state });
}

async function loadState() {
  const { relayState } = await chrome.storage.session.get("relayState");
  if (relayState) state = relayState;
}

async function hasOffscreenDocument() {
  const contexts = await chrome.runtime.getContexts({
    contextTypes: ["OFFSCREEN_DOCUMENT"],
  });
  return contexts.length > 0;
}

async function ensureOffscreenDocument() {
  if (await hasOffscreenDocument()) return;
  await chrome.offscreen.createDocument({
    url: OFFSCREEN_PATH,
    reasons: ["USER_MEDIA", "BLOBS"],
    justification:
      "Mix microphone audio with captured meeting-tab audio, encode the recording, and create a blob: URL for saving it.",
  });
}

async function closeOffscreenDocument() {
  if (await hasOffscreenDocument()) {
    await chrome.offscreen.closeDocument();
  }
}

/** Inject (or re-inject) the visible recording banner into the meeting tab. */
async function showRecordingBanner(tabId) {
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
        const EXISTING_ID = "__relay_recording_banner__";
        if (document.getElementById(EXISTING_ID)) return;

        const banner = document.createElement("div");
        banner.id = EXISTING_ID;
        banner.textContent = "● Relay is recording this meeting";
        banner.style.position = "fixed";
        banner.style.top = "12px";
        banner.style.left = "50%";
        banner.style.transform = "translateX(-50%)";
        banner.style.zIndex = "2147483647";
        banner.style.background = "#1a1a1a";
        banner.style.color = "#ff4d4d";
        banner.style.fontFamily =
          "system-ui, -apple-system, Segoe UI, Roboto, sans-serif";
        banner.style.fontSize = "13px";
        banner.style.fontWeight = "600";
        banner.style.letterSpacing = "0.02em";
        banner.style.padding = "8px 16px";
        banner.style.borderRadius = "999px";
        banner.style.border = "1px solid #ff4d4d";
        banner.style.boxShadow = "0 4px 16px rgba(0,0,0,0.35)";
        banner.style.pointerEvents = "none";
        banner.style.userSelect = "none";
        document.documentElement.appendChild(banner);
      },
    });
  } catch (e) {
    // Tab may not allow injection (e.g., chrome:// or a closed tab). Non-fatal:
    // the native Chrome tab-capture indicator still shows regardless.
    console.warn("Relay: could not inject recording banner:", e);
  }
}

/** Remove the visible recording banner from the meeting tab, if present. */
async function hideRecordingBanner(tabId) {
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
        const el = document.getElementById("__relay_recording_banner__");
        if (el) el.remove();
      },
    });
  } catch (e) {
    // Tab may already be closed — nothing to clean up.
  }
}

/** Start recording the given tab's audio + the user's microphone. */
async function startRecording(tabId) {
  if (state.status !== "idle") {
    return { ok: false, error: "Already recording." };
  }

  const tab = await chrome.tabs.get(tabId).catch(() => null);
  if (!tab) {
    return { ok: false, error: "Could not find the meeting tab." };
  }

  const platform = getMeetingPlatformForUrl(tab.url || "");
  if (!platform) {
    return { ok: false, error: "This tab is not a recognized meeting tab." };
  }

  // Get a tabCapture media stream id usable from the offscreen document.
  let streamId;
  try {
    streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tabId });
  } catch (e) {
    return {
      ok: false,
      error:
        "Couldn't capture this meeting tab's audio. Make sure the tab is active and try again.",
    };
  }

  await ensureOffscreenDocument();

  const response = await chrome.runtime
    .sendMessage({
      target: "offscreen",
      type: "start-recording",
      streamId,
    })
    .catch((e) => ({ ok: false, error: e.message }));

  if (!response || !response.ok) {
    await closeOffscreenDocument();
    return {
      ok: false,
      error:
        (response && response.error) ||
        "Recording failed to start (microphone permission may have been denied).",
    };
  }

  state = {
    status: "recording",
    tabId,
    startedAt: Date.now(),
    lastError: null,
  };
  await persistState();
  await showRecordingBanner(tabId);

  return { ok: true };
}

function formatTimestampForFilename(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `_${pad(date.getHours())}-${pad(date.getMinutes())}-${pad(date.getSeconds())}`
  );
}

/** Stop recording, retrieve the file, and trigger Save As. */
async function stopRecording(reason = "user") {
  if (state.status !== "recording") {
    return { ok: false, error: "Not currently recording." };
  }

  const recordedTabId = state.tabId;
  state.status = "stopping";
  await persistState();

  const response = await chrome.runtime
    .sendMessage({ target: "offscreen", type: "stop-recording" })
    .catch((e) => ({ ok: false, error: e.message }));

  if (recordedTabId !== null) await hideRecordingBanner(recordedTabId);

  if (!response || !response.ok || !response.blobUrl) {
    await closeOffscreenDocument();
    state = { status: "idle", tabId: null, startedAt: null, lastError: "Recording could not be saved." };
    await persistState();
    return { ok: false, error: response && response.error ? response.error : "Recording could not be saved." };
  }

  const filename = `Relay_Meeting_${formatTimestampForFilename(new Date())}.webm`;

  // ================================================================
  // MERGE WITH RELAYSTT: Send audio to local Flask server instead of saving
  // ================================================================
  try {
    // 1. Fetch the actual audio data from the blob URL
    const blobResponse = await fetch(response.blobUrl);
    const audioBlob = await blobResponse.blob();

    // 2. Prepare the FormData (field name MUST be "audio")
    const formData = new FormData();
    formData.append('audio', audioBlob, filename);

    // 3. Upload to your Flask server
    const serverResponse = await fetch('http://localhost:5000/upload', {
      method: 'POST',
      body: formData,
    });

    const data = await serverResponse.json();

    if (data.transcript) {
      console.log('✅ TRANSCRIPT:', data.transcript);
      // Show a nice notification that it worked
      chrome.notifications.create({
        type: 'basic',
        iconUrl: 'icons/icon128.png',
        title: 'Relay STT',
        message: `Transcription done! ${data.transcript.substring(0, 60)}...`,
      });
    } else {
      console.error('Server error:', data.error);
      chrome.notifications.create({
        type: 'basic',
        iconUrl: 'icons/icon128.png',
        title: 'Relay STT Error',
        message: data.error || 'Transcription failed.',
      });
    }
  } catch (e) {
    console.error('Could not reach Flask server. Is it running?', e);
    chrome.notifications.create({
      type: 'basic',
      iconUrl: 'icons/icon128.png',
      title: 'Relay STT Error',
      message: 'Server not found. Run: python app.py',
    });
    // Optional fallback: uncomment the line below if you want it to still save locally when server is down
    // await chrome.downloads.download({ url: response.blobUrl, filename, saveAs: true });
  }

  // Clean up the offscreen document (revokes the blob URL)
  await closeOffscreenDocument();

  state = { status: "idle", tabId: null, startedAt: null, lastError: null };
  await persistState();

  if (reason === "tab-closed") {
    chrome.notifications.create({
      type: "basic",
      iconUrl: "icons/icon128.png",
      title: "Relay",
      message:
        "The meeting tab was closed. Your recording up to that point has been saved.",
    });
  }

  return { ok: true };
}

// --- Message routing from popup ---
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.target !== "background") return;

  (async () => {
    await loadState();

    switch (message.type) {
      case "get-state": {
        sendResponse({ ok: true, state });
        break;
      }
      case "start-recording": {
        const result = await startRecording(message.tabId);
        sendResponse(result);
        break;
      }
      case "stop-recording": {
        const result = await stopRecording("user");
        sendResponse(result);
        break;
      }
      default:
        sendResponse({ ok: false, error: "Unknown message type." });
    }
  })();

  return true; // keep the message channel open for the async response
});

// --- Detect the recorded tab closing mid-recording ---
chrome.tabs.onRemoved.addListener(async (tabId) => {
  await loadState();
  if (state.status === "recording" && state.tabId === tabId) {
    // Leave state.tabId set so stopRecording() still knows which tab id
    // to (attempt to) clean up after — chrome.tabs.get will simply fail
    // for a closed tab, which hideRecordingBanner already handles safely.
    await stopRecording("tab-closed");
  }
});

// Restore in-memory state if the service worker was restarted, and verify
// the recorded tab actually still exists. If the service worker was evicted
// (Chrome does this aggressively after ~30s idle) and the tab closed during
// that window, the tabs.onRemoved listener above never had a chance to fire,
// and naively trusting persisted "recording" state would leave the popup
// stuck claiming a recording is active when nothing is actually running.
loadState().then(async () => {
  if (state.status === "recording" && state.tabId !== null) {
    const tab = await chrome.tabs.get(state.tabId).catch(() => null);
    if (!tab) {
      state = { status: "idle", tabId: null, startedAt: null, lastError: null };
      await persistState();
    }
  }
});
