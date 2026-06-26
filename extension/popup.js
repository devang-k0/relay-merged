/**
 * popup.js
 *
 * Drives the popup UI. The popup itself holds no recording state —
 * it always asks background.js for the current truth, so closing and
 * reopening the popup mid-recording reflects reality correctly.
 */

const recordButton = document.getElementById("recordButton");
const recordLabel = recordButton.querySelector(".record-label");
const timerEl = document.getElementById("timer");
const errorArea = document.getElementById("errorArea");
const errorText = document.getElementById("errorText");
const notMeetingState = document.getElementById("notMeetingState");
const mainState = document.getElementById("mainState");
const statusPill = document.getElementById("statusPill");
const platformNameEl = document.getElementById("platformName");

let timerInterval = null;
let currentTabId = null;

function showError(message) {
  errorText.textContent = message;
  errorArea.classList.remove("hidden");
}

function clearError() {
  errorText.textContent = "";
  errorArea.classList.add("hidden");
}

function formatDuration(ms) {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(minutes)}:${pad(seconds)}`;
}

function startTimer(startedAt) {
  stopTimer();
  timerEl.classList.remove("hidden");
  const tick = () => {
    timerEl.textContent = formatDuration(Date.now() - startedAt);
  };
  tick();
  timerInterval = setInterval(tick, 1000);
}

function stopTimer() {
  if (timerInterval) {
    clearInterval(timerInterval);
    timerInterval = null;
  }
  timerEl.classList.add("hidden");
  timerEl.textContent = "00:00";
}

function setButtonRecording(isRecording) {
  recordButton.disabled = false;
  if (isRecording) {
    recordLabel.textContent = "Stop Recording";
    recordButton.classList.add("recording");
    statusPill.classList.remove("hidden");
  } else {
    recordLabel.textContent = "Start Recording";
    recordButton.classList.remove("recording");
    statusPill.classList.add("hidden");
  }
}

async function sendToBackground(message) {
  return chrome.runtime.sendMessage({ target: "background", ...message });
}

/** Figure out if the active tab is a recognized meeting tab, and render accordingly. */
async function initialize() {
  clearError();

  const [activeTab] = await chrome.tabs.query({
    active: true,
    currentWindow: true,
  });

  const stateResponse = await sendToBackground({ type: "get-state" });
  const state = stateResponse && stateResponse.ok ? stateResponse.state : null;

  const isCurrentlyRecording = state && state.status === "recording";

  // If we're already recording some tab, the popup should reflect THAT
  // tab's recording state even if the user has since switched tabs.
  if (isCurrentlyRecording) {
    currentTabId = state.tabId;
    notMeetingState.classList.add("hidden");
    mainState.classList.remove("hidden");
    setButtonRecording(true);
    startTimer(state.startedAt);

    const recordedTab = await chrome.tabs.get(state.tabId).catch(() => null);
    const platform = recordedTab
      ? getMeetingPlatformForUrl(recordedTab.url || "")
      : null;
    platformNameEl.textContent = platform || "Meeting";
    return;
  }

  // Not recording: base the UI on whether the *active* tab is a meeting tab.
  const platform = activeTab ? getMeetingPlatformForUrl(activeTab.url || "") : null;

  if (!platform) {
    notMeetingState.classList.remove("hidden");
    mainState.classList.add("hidden");
    statusPill.classList.add("hidden");
    return;
  }

  currentTabId = activeTab.id;
  notMeetingState.classList.add("hidden");
  mainState.classList.remove("hidden");
  platformNameEl.textContent = platform;
  setButtonRecording(false);
  stopTimer();

  if (state && state.lastError) {
    showError(state.lastError);
  }
}

async function handleRecordButtonClick() {
  clearError();
  recordButton.disabled = true;

  const stateResponse = await sendToBackground({ type: "get-state" });
  const state = stateResponse && stateResponse.ok ? stateResponse.state : null;
  const isCurrentlyRecording = state && state.status === "recording";

  if (isCurrentlyRecording) {
    const result = await sendToBackground({ type: "stop-recording" });
    if (!result || !result.ok) {
      showError((result && result.error) || "Could not stop recording.");
      recordButton.disabled = false;
      return;
    }
    stopTimer();
    setButtonRecording(false);
    return;
  }

  if (currentTabId === null) {
    showError("No meeting tab detected.");
    recordButton.disabled = false;
    return;
  }

  const result = await sendToBackground({
    type: "start-recording",
    tabId: currentTabId,
  });

  if (!result || !result.ok) {
    showError((result && result.error) || "Could not start recording.");
    recordButton.disabled = false;
    return;
  }

  setButtonRecording(true);
  startTimer(Date.now());
}

recordButton.addEventListener("click", handleRecordButtonClick);
initialize();
