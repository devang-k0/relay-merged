/**
 * config.js
 *
 * Central, easy-to-edit list of "meeting tab" URL patterns.
 * Used by background.js (to decide if a tab is recordable) and
 * popup.js (to decide whether to enable the Start button).
 *
 * To add a new meeting platform, just add another entry here —
 * nothing else in the codebase needs to change.
 */

// Each entry is a simple hostname-matching rule. We keep this
// intentionally simple (substring / suffix matching) rather than
// full match-pattern syntax, so it's trivial to read and extend.
const MEETING_HOST_RULES = [
  { name: "Google Meet", test: (host) => host === "meet.google.com" },
  { name: "Zoom", test: (host) => host === "zoom.us" || host.endsWith(".zoom.us") },
  { name: "Microsoft Teams", test: (host) => host === "teams.microsoft.com" || host.endsWith(".teams.microsoft.com") },
  { name: "Webex", test: (host) => host === "webex.com" || host.endsWith(".webex.com") },
  { name: "GoToMeeting", test: (host) => host === "gotomeeting.com" || host.endsWith(".gotomeeting.com") },
  { name: "BlueJeans", test: (host) => host === "bluejeans.com" || host.endsWith(".bluejeans.com") },
  { name: "Whereby", test: (host) => host === "whereby.com" || host.endsWith(".whereby.com") },
  { name: "Discord", test: (host) => host === "discord.com" },
  { name: "Slack", test: (host) => host === "app.slack.com" || host.endsWith(".slack.com") },
];

/**
 * Returns the matching platform name for a URL, or null if the URL
 * doesn't look like a meeting tab.
 */
function getMeetingPlatformForUrl(urlString) {
  try {
    const url = new URL(urlString);
    if (url.protocol !== "https:" && url.protocol !== "http:") return null;
    const host = url.hostname;
    for (const rule of MEETING_HOST_RULES) {
      if (rule.test(host)) return rule.name;
    }
    return null;
  } catch (e) {
    return null;
  }
}

// Make available to both service worker (importScripts) and popup (script tag).
if (typeof module !== "undefined") {
  module.exports = { getMeetingPlatformForUrl, MEETING_HOST_RULES };
}
