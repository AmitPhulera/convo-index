// Shared helpers for both pages: escaping, formatting, theme, toasts.

function escape(s) {
  return (s == null ? "" : String(s)).replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
const esc = escape;

// Every POST goes through here: the server refuses one without the X-Convo-Index header
// (CSRF guard, see server.py CSRF_HEADER). Returns the fetch promise; callers keep their own
// res.ok / .json() handling.
function apiPost(url, body) {
  const init = { method: "POST", headers: { "X-Convo-Index": "1" } };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  return fetch(url, init);
}

// ~ for the home directory so long project paths stay readable.
function shortPath(p) {
  return (p || "").replace(/^\/(Users|home)\/[^/]+/, "~");
}
function baseName(p) {
  const parts = (p || "").replace(/\/+$/, "").split("/");
  return parts[parts.length - 1] || p || "";
}

// "3h ago", "yesterday", "2w ago" … with the full timestamp available for a title.
function relTime(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  if (isNaN(d)) return ts.slice(0, 10);
  const s = Math.round((Date.now() - d.getTime()) / 1000);
  if (s < 60) return "just now";
  const m = Math.round(s / 60); if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60); if (h < 24) return `${h}h ago`;
  const days = Math.round(h / 24);
  if (days === 1) return "yesterday";
  if (days < 7) return `${days}d ago`;
  if (days < 30) return `${Math.round(days / 7)}w ago`;
  if (days < 365) return `${Math.round(days / 30)}mo ago`;
  return `${Math.round(days / 365)}y ago`;
}
function fullTime(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  return isNaN(d) ? ts : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function fmtTokens(n) {
  n = n || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}
function fmtUsd(x) {
  if (x === null || x === undefined) return "—";
  return x >= 1 ? "$" + x.toLocaleString(undefined, { maximumFractionDigits: 2, minimumFractionDigits: 2 }) : "$" + x.toFixed(4);
}
function fmtInt(n) { return (n || 0).toLocaleString(); }

// Toasts: bottom-right, auto-dismiss. kind: "info" | "success" | "error".
function toast(msg, kind = "info", ms = 4500) {
  const box = document.getElementById("toasts");
  if (!box) return;
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), ms);
}
function showError(msg) { toast(msg, "error", 6000); }

// Theme: the inline <head> script applied the stored choice before paint; the button
// toggles between light and dark and remembers it. Auto (follow the OS) is the default.
(function themeSetup() {
  const root = document.documentElement;
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  const effective = () => root.getAttribute("data-theme") || (media.matches ? "dark" : "light");
  const SUN = '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>';
  const MOON = '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>';
  function paint() {
    const btn = document.getElementById("theme");
    if (!btn) return;
    const svg = btn.querySelector("svg");
    svg.innerHTML = effective() === "dark" ? SUN : MOON;
    btn.title = effective() === "dark" ? "Switch to light theme" : "Switch to dark theme";
  }
  media.addEventListener("change", () => {
    if (!root.getAttribute("data-theme")) root.classList.toggle("dark-auto", media.matches);
    paint();
  });
  document.addEventListener("DOMContentLoaded", () => {
    paint();
    const btn = document.getElementById("theme");
    if (!btn) return;
    btn.addEventListener("click", () => {
      const next = effective() === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      root.classList.remove("dark-auto");
      try { localStorage.setItem("theme", next); } catch (e) { /* private mode */ }
      paint();
    });
  });
})();
