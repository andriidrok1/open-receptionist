/* The owner's dashboard as a React app (React 18 UMD + htm, no build step).
 *
 * Three views. Inbox is the one the page opens on: the owner's to-do feed —
 * jobs to drive out to, numbers to call back, bookings called off — each with
 * a checkmark only a human flips. Calls is the call log with transcripts.
 * Overview is the stats page (KPIs, occupancy, revenue) that used to be the
 * whole dashboard.
 *
 * Every visible string comes from the business's language pack via the API
 * (`business.ui`) — nothing here is allowed to hard-code a language.
 */
/* global React, ReactDOM, htm */

const html = htm.bind(React.createElement);
const { useState, useEffect, useCallback, useRef } = React;

const nf = new Intl.NumberFormat();
const two = (n) => String(n).padStart(2, "0");

/* Today at the BUSINESS, delivered by the server with each inbox poll — the
 * browser's own date is wrong exactly when the owner reads from another
 * timezone. The browser's date fills in only before the first poll lands. */
let BIZ_TODAY = null;

/* "YYYY-MM-DD HH:MM:SS" in the business's timezone → "17:42" today,
 * "12.08, 17:42" otherwise. */
function fmtAt(stamp) {
  if (!stamp) return "";
  const [d, t] = stamp.split(" ");
  const now = new Date();
  const today = BIZ_TODAY || `${now.getFullYear()}-${two(now.getMonth() + 1)}-${two(now.getDate())}`;
  const hm = t ? t.slice(0, 5) : "";
  return d === today ? hm : `${d.slice(8, 10)}.${d.slice(5, 7)}, ${hm}`;
}

const fmtDate = (iso) => `${iso.slice(8, 10)}.${iso.slice(5, 7)}.${iso.slice(0, 4)}`;
const fmtDay = (iso) => `${iso.slice(8, 10)}.${iso.slice(5, 7)}`;

/* A booking's moment, the way the caller heard it: slots get "12.08, 10:00"
 * (or the arrival window "10:00–11:00" when the business promises windows),
 * nights get "12.08 → 15.08". */
function fmtWhen(item, biz) {
  if (!biz.is_slots) return `${fmtDay(item.starts_at)} → ${fmtDay(item.ends_at)}`;
  const day = fmtDay(item.starts_at);
  const start = item.starts_at.slice(11, 16);
  if (biz.arrival_window_min > 0) return `${day}, ${start}–${item.ends_at.slice(11, 16)}`;
  return `${day}, ${start}`;
}

function fmtDuration(s) {
  if (s == null) return "";
  const n = Math.round(s);
  return `${Math.floor(n / 60)}:${two(n % 60)}`;
}

/* "$" reads as "$99", "грн" reads as "99 грн" — symbol currencies go in
 * front, word currencies go after, same as people write them. */
const fmtMoney = (n, cur) =>
  (cur || "").length === 1 ? `${cur}${nf.format(n)}` : `${nf.format(n)} ${cur}`;

/* Agent lines carry TTS pause markup ("<break time='0.4s'/>") — real for the
 * synthesiser, noise for the owner reading the call back. */
const cleanSpeech = (t) => t.replace(/<break[^>]*\/?>/g, " ").replace(/\s{2,}/g, " ").trim();

async function get(url) {
  const r = await fetch(url);
  if (r.status === 401) { location.reload(); throw new Error("unauthorized"); }
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}

async function post(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (r.status === 401) { location.reload(); throw new Error("unauthorized"); }
  return r.ok;
}

/* ------------------------------- icons ------------------------------- */

const PATHS = {
  inbox: "M3 13h5l2 3h4l2-3h5 M5 6h14l2 7v6a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-6z",
  phone: "M4 5c0 9 6 15 15 15l1-4-4-2-2 2c-2-1-4-3-5-5l2-2-2-4z",
  grid: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  calendarCheck: "M8 2v4 M16 2v4 M3 10h18 M3 6a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z M9 15l2 2 4-4",
  calendarX: "M8 2v4 M16 2v4 M3 10h18 M3 6a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z M10 14l4 4 M14 14l-4 4",
  callback: "M4 5c0 9 6 15 15 15l1-4-4-2-2 2c-2-1-4-3-5-5l2-2-2-4z M14 5h6 M17 2v6",
  check: "M4 12l5 5L20 6",
  chat: "M21 12a8 8 0 0 1-8 8H4l2-3a8 8 0 1 1 15-5z",
  mic: "M9 3h6v11a3 3 0 0 1-6 0z M6 11a6 6 0 0 0 12 0 M12 17v4",
  gear: "M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z M19 12a7 7 0 0 0-.1-1l2-1.5-2-3.4-2.3 1a7 7 0 0 0-1.7-1l-.3-2.6h-4l-.3 2.6a7 7 0 0 0-1.7 1l-2.3-1-2 3.4 2 1.5a7 7 0 0 0 0 2l-2 1.5 2 3.4 2.3-1a7 7 0 0 0 1.7 1l.3 2.6h4l.3-2.6a7 7 0 0 0 1.7-1l2.3 1 2-3.4-2-1.5c.1-.3.1-.7.1-1z",
};

function Icon({ d, size = 20 }) {
  return html`<svg viewBox="0 0 24 24" width=${size} height=${size} fill="none"
    stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
    <path d=${PATHS[d]} /></svg>`;
}

/* ------------------------------- shell ------------------------------- */

function NavButton({ icon, label, active, count, onClick }) {
  return html`<button className=${active ? "active" : ""} onClick=${onClick}>
    <${Icon} d=${icon} />${label}
    ${count > 0 && html`<span className="count">${count}</span>`}
  </button>`;
}

/* Two separate truths, one indicator: red = the process is dead (nobody
 * answers), amber = alive but the owner has this shift (dashboard settings),
 * green = the assistant is picking up. */
function dutyColor(online, onDuty) {
  return !online ? "var(--crit)" : !onDuty ? "var(--warn)" : "var(--good)";
}
function dutyLabel(ui, online, onDuty) {
  return !online ? ui.agent_offline : !onDuty ? ui.owner_answering : ui.agent_online;
}

function AgentState({ ui, online, onDuty }) {
  const color = dutyColor(online, onDuty);
  return html`<div className="agent-state" style=${{ color }}>
    <span className="dot" style=${{ background: color }}></span>
    ${dutyLabel(ui, online, onDuty)}
  </div>`;
}

function Sidebar({ biz, ui, view, setView, newCount, online, onDuty }) {
  return html`<aside className="sidebar">
    <div className="brand">
      <div className="crest"><${Icon} d="mic" size=${24} /></div>
      <div className="brand-name">${(biz.brand || "").toUpperCase()}</div>
      <div className="brand-city">${biz.city || ""}</div>
    </div>
    <nav className="nav">
      <${NavButton} icon="inbox" label=${ui.inbox} active=${view === "inbox"}
        count=${newCount} onClick=${() => setView("inbox")} />
      <${NavButton} icon="phone" label=${ui.calls} active=${view === "calls"}
        onClick=${() => setView("calls")} />
      <${NavButton} icon="grid" label=${ui.overview} active=${view === "overview"}
        onClick=${() => setView("overview")} />
      <${NavButton} icon="gear" label=${ui.settings} active=${view === "settings"}
        onClick=${() => setView("settings")} />
    </nav>
    <div className="agent-card">
      <div className="agent-row">
        <div className="agent-mic"><${Icon} d="mic" /></div>
        <div>
          <div className="agent-role">${ui.agent_role}</div>
          <div className="agent-name">${biz.agent}</div>
          <${AgentState} ui=${ui} online=${online} onDuty=${onDuty} />
        </div>
      </div>
      ${biz.auth && html`<a className="signout" href="/logout">${ui.sign_out}</a>`}
    </div>
  </aside>`;
}

function TabBar({ ui, view, setView, newCount }) {
  const tab = (key, icon, label, count) => html`
    <button className=${view === key ? "active" : ""} onClick=${() => setView(key)}>
      ${count > 0 && html`<span className="count">${count}</span>`}
      <${Icon} d=${icon} size=${22} />${label}
    </button>`;
  return html`<nav className="tabbar">
    ${tab("inbox", "inbox", ui.inbox, newCount)}
    ${tab("calls", "phone", ui.calls, 0)}
    ${tab("overview", "grid", ui.overview, 0)}
    ${tab("settings", "gear", ui.settings, 0)}
  </nav>`;
}

/* ------------------------------- inbox ------------------------------- */

const KIND = {
  booking: { icon: "calendarCheck", bg: "var(--good-soft)", fg: "var(--good-ink)" },
  cancellation: { icon: "calendarX", bg: "var(--crit-soft)", fg: "var(--crit)" },
  message: { icon: "callback", bg: "var(--warn-soft)", fg: "#b56a15" },
};

function Details({ details, labels }) {
  const rows = Object.entries(details || {}).filter(([, v]) => v !== "" && v != null);
  if (!rows.length) return null;
  return rows.map(
    ([k, v]) => html`<div className="item-line" key=${k}>
      ${labels[k] || k}: <b>${v === true ? "✓" : v === false ? "✗" : String(v)}</b>
    </div>`
  );
}

function CancelButton({ item, ui, onCancel }) {
  // Two taps on purpose: one tap arms it ("Confirm cancellation"), the second
  // cancels, and three idle seconds disarm it — a modal would be sturdier but
  // heavier than this deserves, and a single tap on a phone is an accident.
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 3000);
    return () => clearTimeout(t);
  }, [armed]);
  return html`<button className=${"cancelbtn" + (armed ? " armed" : "")}
    onClick=${() => (armed ? onCancel(item) : setArmed(true))}>
    ${armed ? ui.confirm_cancel : ui.cancel_booking}
  </button>`;
}

function InboxItem({ item, biz, ui, onHandled, onViewCall, onCancel }) {
  const kind = KIND[item.type];
  const kindLabel = { booking: ui.item_booking, cancellation: ui.item_cancellation, message: ui.item_message }[item.type];
  return html`<div className=${"item" + (item.handled ? " done" : "")}>
    <div className="item-ic" style=${{ background: kind.bg, color: kind.fg }}>
      <${Icon} d=${kind.icon} size=${21} />
    </div>
    <div className="item-body">
      <div className="item-top">
        <span className="item-kind" style=${{ color: kind.fg }}>${kindLabel}</span>
        ${item.ref_code && html`<span className="item-when" style=${{ marginLeft: 0 }}>${item.ref_code}</span>`}
        <span className="item-when">${fmtAt(item.at)}</span>
      </div>
      <div className="item-name">${item.customer_name}</div>
      ${item.type === "message"
        ? html`<div className="item-line">${item.topic}</div>`
        : html`
          <div className="item-line">
            ${item.resource_label} · <b>${fmtWhen(item, biz)}</b>
            ${biz.labels.party && item.party_size > 1 ? ` · ${item.party_size}` : ""}
            ${" · "}<span className="item-price">${fmtMoney(item.total_price, biz.currency)}</span>
          </div>
          <${Details} details=${item.details} labels=${biz.intake_labels} />`}
      <a className="tel" href=${"tel:" + item.phone}><${Icon} d="phone" size=${15} />${item.phone}</a>
      <div className="item-foot">
        ${item.call_id && html`
          <button className="viewcall" onClick=${() => onViewCall(item.call_id)}>
            <${Icon} d="chat" size=${14} />${ui.view_call}
          </button>`}
        ${item.type === "booking" && html`<${CancelButton} item=${item} ui=${ui} onCancel=${onCancel} />`}
        <button className=${"check" + (item.handled ? " on" : "")}
          onClick=${() => onHandled(item, !item.handled)}>
          <${Icon} d="check" size=${15} />
          ${item.handled ? ui.handled_done : ui.mark_handled}
        </button>
      </div>
    </div>
  </div>`;
}

/* Does an inbox item match what the owner typed? Names and addresses match as
 * substrings; anything with digits in it also matches against the phone with
 * the punctuation stripped, so "415 729" finds "+14157298634". */
function matches(item, query) {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  const hay = [
    item.customer_name, item.phone, item.ref_code, item.topic,
    item.resource_label, ...Object.values(item.details || {}),
  ].filter((x) => typeof x === "string").join(" ").toLowerCase();
  if (hay.includes(q)) return true;
  const digits = q.replace(/\D/g, "");
  return digits.length >= 3 && (item.phone || "").replace(/\D/g, "").includes(digits);
}

function Inbox({ items, biz, ui, onHandled, onViewCall, onCancel }) {
  const [filter, setFilter] = useState("new");
  const [query, setQuery] = useState("");
  const found = items.filter((x) => matches(x, query));
  const fresh = found.filter((x) => !x.handled);
  // A search means "find it wherever it is" — it looks through everything,
  // not through the New pile the chips happen to be on.
  const shown = query.trim() ? found : filter === "new" ? fresh : found;
  return html`<div>
    <div className="chips">
      <button className=${"chip" + (filter === "new" && !query.trim() ? " active" : "")}
        onClick=${() => { setFilter("new"); setQuery(""); }}>${ui.new_items}${fresh.length ? ` · ${fresh.length}` : ""}</button>
      <button className=${"chip" + (filter === "all" || query.trim() ? " active" : "")}
        onClick=${() => setFilter("all")}>${ui.all_items}${items.length ? ` · ${items.length}` : ""}</button>
      <input className="search" type="search" placeholder=${ui.search_placeholder}
        value=${query} onInput=${(e) => setQuery(e.target.value)} />
    </div>
    ${shown.length
      ? html`<div className="feed">
          ${shown.map((item) => html`<${InboxItem} key=${item.type + item.id + item.handled}
            item=${item} biz=${biz} ui=${ui} onHandled=${onHandled}
            onViewCall=${onViewCall} onCancel=${onCancel} />`)}
        </div>`
      : html`<div className="empty">
          <${Icon} d=${query.trim() ? "inbox" : items.length ? "check" : "inbox"} size=${34} />
          <div>${query.trim() ? ui.no_results : items.length ? ui.all_handled : ui.inbox_empty}</div>
        </div>`}
  </div>`;
}

/* ------------------------------- calls ------------------------------- */

function CallRow({ call, ui, onOpen }) {
  const badge =
    call.disposition === "booked"
      ? html`<span className="badge b-good">${ui.outcome_booked}${call.ref_code ? ` · ${call.ref_code}` : ""}</span>`
      : call.disposition === "message"
        ? html`<span className="badge b-warn">${ui.outcome_message}</span>`
        : html`<span className="badge b-mute">${ui.outcome_none}</span>`;
  const meta = [
    call.duration_s != null && fmtDuration(call.duration_s),
    call.replies != null && `${call.replies} ${ui.replies_label}`,
    call.cost && call.cost.total != null && `${call.cost.total} ${call.cost.currency || ""}`,
  ].filter(Boolean).join(" · ");
  return html`<button className="callrow" onClick=${() => onOpen(call.call_id)}>
    <div className="call-ic"><${Icon} d="phone" size=${18} /></div>
    <div>
      <div className="call-when">${fmtAt(call.at)}</div>
      <div className="call-meta">${meta}</div>
    </div>
    ${badge}
  </button>`;
}

function Calls({ calls, ui, onOpen }) {
  if (!calls.length) {
    return html`<div className="empty" style=${{ marginTop: 18 }}>
      <${Icon} d="phone" size=${34} /><div>${ui.calls_empty}</div>
    </div>`;
  }
  return html`<div className="calls">
    ${calls.map((c) => html`<${CallRow} key=${c.call_id} call=${c} ui=${ui} onOpen=${onOpen} />`)}
  </div>`;
}

function Transcript({ callId, ui, onClose }) {
  const [call, setCall] = useState(null);
  useEffect(() => { get(`/api/call/${callId}`).then(setCall).catch(() => onClose()); }, [callId]);
  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && onClose();
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, []);
  return html`
    <div className="scrim" onClick=${onClose}></div>
    <div className="drawer">
      <div className="drawer-h">
        <div className="call-ic"><${Icon} d="phone" size=${18} /></div>
        <div>
          <div className="call-when">${call ? fmtAt(call.at) : "…"}</div>
          <div className="call-meta">${call && call.metrics.duration_s != null ? fmtDuration(call.metrics.duration_s) : ""}</div>
        </div>
        <button className="x" onClick=${onClose} aria-label=${ui.close}>✕</button>
      </div>
      <div className="drawer-body">
        ${call && call.lines.map((line, i) =>
          line.who === "tool"
            ? html`<div className="bub-tool" key=${i}>· ${line.text}</div>`
            : html`<div className=${"bubble " + (line.who === "agent" ? "bub-agent" : "bub-caller")} key=${i}>
                ${cleanSpeech(line.text)}
              </div>`
        )}
      </div>
    </div>`;
}

/* ------------------------------ overview ------------------------------ */

const STATUS = { good: "var(--good)", warning: "var(--warn)", critical: "var(--crit)", closed: "var(--muted)" };
// Badge colours are dealt by resource order in the config, so a new industry
// never needs a front-end change.
const PALETTE = [
  ["#eef1ff", "#5566d8"], ["#e7f0fe", "#2f79d6"], ["#fff1e3", "#e08636"],
  ["#fde9e9", "#dc4b4b"], ["#e8f7ee", "#2f9e5e"], ["#f3ecfd", "#7b5bd6"],
];

function Kpis({ stats, biz, ui }) {
  const meta = [
    { key: "total_bookings", label: `${biz.labels.action || ui.records} ${ui.total_suffix}`, color: "#6c8cf5",
      icon: "M8 2v4 M16 2v4 M3 10h18 M3 6a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" },
    { key: "total_revenue", label: `${ui.revenue} (${biz.currency})`, color: "#22b07d",
      icon: "M2 8.5A2.5 2.5 0 0 1 4.5 6h15A2.5 2.5 0 0 1 22 8.5v8a2.5 2.5 0 0 1-2.5 2.5h-15A2.5 2.5 0 0 1 2 16.5z M2 10h20 M16 14.5h2" },
  ];
  if (biz.labels.party) meta.push({
    key: "total_people", label: `${biz.labels.party} ${ui.total_suffix}`, color: "#f0913a",
    icon: "M9 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6z M3 20c0-3 3-5 6-5s6 2 6 5 M16 6a3 3 0 0 1 0 6 M18 20c0-2-1-3.5-2.5-4.3" });
  meta.push({ key: "avg_check", label: `${ui.avg_check} (${biz.currency})`, color: "#7c6cf0",
    icon: "M2 7.5A2.5 2.5 0 0 1 4.5 5h15A2.5 2.5 0 0 1 22 7.5v9a2.5 2.5 0 0 1-2.5 2.5h-15A2.5 2.5 0 0 1 2 16.5z M2 9h20 M6 15h4" });

  return html`<section className="kpis">
    ${meta.map((m) => {
      const d = stats.deltas[m.key];
      return html`<div className="kpi" key=${m.key}>
        <div className="kpi-ic" style=${{ background: m.color }}>
          <svg viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="1.7"
            strokeLinecap="round" strokeLinejoin="round"><path d=${m.icon} /></svg>
        </div>
        <div>
          <div className="kpi-label">${m.label}</div>
          <div className="kpi-val">${nf.format(stats.kpi[m.key])}</div>
          <div className="kpi-delta">
            ${d == null
              ? html`<span style=${{ color: "var(--muted)" }}>${ui.no_previous}</span>`
              : html`<span className=${d >= 0 ? "up" : "down"}>${d >= 0 ? "+" : ""}${d}%</span> ${ui.vs_previous}`}
          </div>
        </div>
      </div>`;
    })}
  </section>`;
}

function Occupancy({ rows, biz, ui }) {
  return html`<div className="card">
    <div className="card-h"><div className="card-title">${ui.occupancy}</div></div>
    <div className="occ-head">
      <div>${biz.labels.resource || ui.occupancy}</div><div></div>
      <div style=${{ textAlign: "right" }}>${ui.occ_busy}</div>
      <div style=${{ textAlign: "right" }}>${ui.total}</div>
    </div>
    ${rows.map((t) => html`<div className="occ-row" key=${t.resource}>
      <div className="occ-name">${t.label}</div>
      <div className="occ-track">
        <div className="occ-fill" style=${{ width: `${Math.min(t.occupancy_pct, 100)}%`, background: STATUS[t.status] }}></div>
      </div>
      <div className="occ-count">${t.booked} / ${t.capacity}</div>
      <div className="occ-pct" style=${{ color: STATUS[t.status] }}>${t.occupancy_pct}%</div>
    </div>`)}
    <div className="legend">
      <span><i style=${{ background: "var(--good)" }}></i>${ui.free}</span>
      <span><i style=${{ background: "var(--warn)" }}></i>${ui.few}</span>
      <span><i style=${{ background: "var(--crit)" }}></i>${ui.none_left}</span>
      <span><i style=${{ background: "var(--muted)" }}></i>${ui.closed_today}</span>
    </div>
  </div>`;
}

function RevenueChart({ rows, biz, ui }) {
  const max = Math.max(1, ...rows.map((t) => t.revenue));
  const unit = max > 200000 ? 50000 : max > 20000 ? 5000 : 500;
  const step = Math.max(unit, Math.ceil(max / 4 / unit) * unit);
  const niceMax = step * 4;
  const marks = [4, 3, 2, 1, 0];
  return html`<div className="card">
    <div className="card-h"><div className="card-title">${ui.revenue} (${biz.currency})</div></div>
    <div className="chart">
      <div className="yaxis">
        ${marks.map((i) => {
          const val = step * i;
          return html`<div key=${i}>${val === 0 ? "0" : val >= 1000 ? `${val / 1000} ${ui.thousands}` : val}</div>`;
        })}
      </div>
      <div className="plotwrap">
        <div className="plot">
          <div>${marks.map((i) => html`<div className="gridline" key=${i} style=${{ top: `${(1 - i / 4) * 100}%` }}></div>`)}</div>
          <div className="bars">
            ${rows.map((t) => html`<div className="barcol" key=${t.resource} title=${`${t.label}: ${nf.format(t.revenue)} ${biz.currency}`}>
              <div className="barval">${nf.format(t.revenue)}</div>
              <div className="bar" style=${{ height: `${(t.revenue / niceMax) * 100}%` }}></div>
            </div>`)}
          </div>
        </div>
        <div className="xaxis">${rows.map((t) => html`<span key=${t.resource}>${t.label}</span>`)}</div>
      </div>
    </div>
  </div>`;
}

function BookingsTable({ stats, biz, ui }) {
  const colors = {};
  stats.by_resource.forEach((t, i) => { colors[t.resource] = PALETTE[i % PALETTE.length]; });
  const cols = ["№", biz.labels.customer || ui.customer, ui.phone, biz.labels.resource || ui.occupancy];
  cols.push(biz.is_slots ? ui.datetime_col : ui.dates_col);
  cols.push(biz.is_slots ? ui.duration : biz.labels.unit || ui.units);
  if (biz.labels.party) cols.push(biz.labels.party);
  cols.push(`${ui.amount} (${biz.currency})`, ui.created);
  const shown = stats.bookings.slice(0, 8);
  return html`<section className="card" style=${{ marginTop: 18 }}>
    <div className="card-h"><div className="card-title">${biz.labels.action || ui.records}</div></div>
    <div className="tablewrap">
      <table>
        <thead><tr>${cols.map((c, i) => html`<th key=${i}>${c}</th>`)}</tr></thead>
        <tbody>
          ${shown.map((b) => {
            const [bg, fg] = colors[b.resource] || ["#eee", "#555"];
            const when = biz.is_slots
              ? `${fmtDate(b.starts_at)}, ${b.starts_at.slice(11, 16)}`
              : html`${fmtDate(b.starts_at)}<span className="arrow">→</span>${fmtDate(b.ends_at)}`;
            const amount = biz.is_slots
              ? `${Math.round((new Date(b.ends_at) - new Date(b.starts_at)) / 60000)} ${ui.minutes_short}`
              : b.units;
            return html`<tr key=${b.ref_code}>
              <td className="ref">${b.ref_code}</td>
              <td>${b.customer_name}</td>
              <td className="num">${b.phone}</td>
              <td><span className="rbadge" style=${{ background: bg, color: fg }}>${b.resource_label}</span></td>
              <td className="dates num">${when}</td>
              <td className="num">${amount}</td>
              ${biz.labels.party && html`<td className="num">${b.party_size}</td>`}
              <td className="num" style=${{ fontWeight: 600 }}>${nf.format(b.total_price)}</td>
              <td className="dates num">${fmtAt(b.created_at)}</td>
            </tr>`;
          })}
        </tbody>
      </table>
    </div>
    <div className="tfoot"><div>${ui.showing} ${shown.length} ${ui.of} ${stats.bookings.length}</div></div>
  </section>`;
}

function Overview({ stats, biz, ui }) {
  if (!stats) return html`<div className="empty" style=${{ marginTop: 18 }}>…</div>`;
  return html`<div>
    <${Kpis} stats=${stats} biz=${biz} ui=${ui} />
    <section className="grid2">
      <${Occupancy} rows=${stats.by_resource} biz=${biz} ui=${ui} />
      <${RevenueChart} rows=${stats.by_resource} biz=${biz} ui=${ui} />
    </section>
    <${BookingsTable} stats=${stats} biz=${biz} ui=${ui} />
  </div>`;
}

/* ------------------------------ settings ------------------------------ */

const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];

function Notifications({ ui }) {
  const [tg, setTg] = useState(null);
  useEffect(() => {
    get("/api/settings").then((r) => setTg(r.telegram)).catch(() => {});
  }, []);
  if (!tg) return null;

  const unlink = async () => {
    const r = await fetch("/api/telegram/unlink", { method: "POST" });
    if (r.ok) setTg((await r.json()).telegram);
  };

  return html`<div className="card settings-card">
    <div className="card-title">${ui.notifications}</div>
    ${!tg.configured && html`<div className="hint">${ui.notif_no_token}</div>`}
    ${tg.configured && html`
      <div className="hint">${ui.notif_link_hint}</div>
      <div className="savebar" style=${{ marginTop: 10 }}>
        <a className="savebtn" style=${{ textDecoration: "none" }}
           href=${"https://t.me/" + tg.bot + "?start=" + tg.pair_code}
           target="_blank" rel="noreferrer">${ui.connect_telegram}</a>
        <span className="savemsg">${(ui.notif_connected || "{n}").replace("{n}", tg.chats)}</span>
        ${tg.chats > 0 && html`<button className="chip" onClick=${unlink}>${ui.unlink}</button>`}
      </div>`}
  </div>`;
}

function Settings({ biz, ui, onSaved }) {
  const [schedule, setSchedule] = useState(null);
  const [status, setStatus] = useState("");
  useEffect(() => {
    get("/api/settings").then((r) => setSchedule(r.schedule)).catch(() => {});
  }, []);
  if (!schedule) return null;

  const names = biz.weekdays_short || DAYS;
  const setDay = (d, patch) =>
    setSchedule({
      ...schedule,
      owner_hours: { ...schedule.owner_hours, [d]: { ...schedule.owner_hours[d], ...patch } },
    });

  const save = async () => {
    setStatus("…");
    const ok = await post("/api/settings", schedule);
    setStatus(ok ? ui.saved : ui.save_failed);
    if (ok) onSaved();
    setTimeout(() => setStatus(""), 3000);
  };

  const modes = [["always", ui.mode_always], ["schedule", ui.mode_schedule], ["off", ui.mode_off]];
  return html`<div className="card settings-card">
    <div className="card-title">${ui.bot_answers}</div>
    <div className="chips" style=${{ margin: "14px 0" }}>
      ${modes.map(([key, label]) => html`<button key=${key}
        className=${"chip" + (schedule.mode === key ? " active" : "")}
        onClick=${() => setSchedule({ ...schedule, mode: key })}>${label}</button>`)}
    </div>
    ${schedule.mode === "schedule" && html`
      <div className="hint">${ui.schedule_hint}</div>
      <div>
        ${DAYS.map((d, i) => {
          const span = schedule.owner_hours[d];
          return html`<div className=${"dayrow" + (span.enabled ? "" : " off")} key=${d}>
            <label className="dayname">
              <input type="checkbox" checked=${span.enabled}
                onChange=${(e) => setDay(d, { enabled: e.target.checked })} />
              ${names[i]}
            </label>
            <span className="daylabel">${span.enabled ? ui.i_answer : ""}</span>
            <input type="time" value=${span.from} disabled=${!span.enabled}
              onInput=${(e) => setDay(d, { from: e.target.value })} />
            <span className="daylabel">${ui.to_label}</span>
            <input type="time" value=${span.to} disabled=${!span.enabled}
              onInput=${(e) => setDay(d, { to: e.target.value })} />
          </div>`;
        })}
      </div>`}
    <div className="savebar">
      <button className="savebtn" onClick=${save}>${ui.save}</button>
      <span className="savemsg">${status}</span>
    </div>
  </div>`;
}

/* -------------------------------- app -------------------------------- */

function App() {
  const [biz, setBiz] = useState(null);
  const [items, setItems] = useState([]);
  const [calls, setCalls] = useState([]);
  const [stats, setStats] = useState(null);
  const [view, setView] = useState("inbox");
  const [openCall, setOpenCall] = useState(null);
  const [online, setOnline] = useState(true);
  const [onDuty, setOnDuty] = useState(true);
  const viewRef = useRef(view);
  viewRef.current = view;

  const refresh = useCallback(async () => {
    const inbox = await get("/api/inbox");
    BIZ_TODAY = inbox.today || BIZ_TODAY;
    setBiz(inbox.business);
    setItems(inbox.items);
    setOnline(inbox.agent_online !== false);
    setOnDuty(inbox.agent_on_duty !== false);
    if (viewRef.current === "calls") setCalls((await get("/api/calls")).calls);
    if (viewRef.current === "overview") setStats(await get("/api/stats"));
  }, []);

  useEffect(() => {
    refresh().catch(() => {});
    const t = setInterval(() => refresh().catch(() => {}), 10000);
    return () => clearInterval(t);
  }, []);

  // Switching views fetches that view's data immediately rather than waiting
  // out the poll interval.
  useEffect(() => {
    if (view === "calls") get("/api/calls").then((r) => setCalls(r.calls)).catch(() => {});
    if (view === "overview") get("/api/stats").then(setStats).catch(() => {});
  }, [view]);

  const newCount = items.filter((x) => !x.handled).length;
  useEffect(() => {
    if (!biz) return;
    document.title = (newCount ? `(${newCount}) ` : "") + biz.brand;
  }, [newCount, biz]);

  const onHandled = useCallback(async (item, handled) => {
    // Optimistic: the tick lands instantly, the server is told in the
    // background, and a failure puts the truth back on the next refresh.
    setItems((prev) => prev.map((x) => (x.id === item.id && x.type === item.type ? { ...x, handled } : x)));
    const ok = await post("/api/handled", { kind: item.type, id: item.id, handled });
    if (!ok) refresh().catch(() => {});
  }, [refresh]);

  const onCancel = useCallback(async (item) => {
    // NOT optimistic, unlike the checkmark: a checkmark is the owner's own
    // note, a cancellation frees a slot other callers can immediately take.
    // The card flips only once the database says it did.
    const ok = await post("/api/cancel", { ref_code: item.ref_code });
    if (!ok) alert(biz?.ui?.cancel_failed || "error");
    refresh().catch(() => {});
  }, [refresh, biz]);

  if (!biz) return null;
  const ui = biz.ui;
  const title = { inbox: ui.inbox, calls: ui.calls, overview: ui.overview, settings: ui.settings }[view];

  return html`<div className="app">
    <${Sidebar} biz=${biz} ui=${ui} view=${view} setView=${setView} newCount=${newCount}
      online=${online} onDuty=${onDuty} />
    <main className="main">
      <div className="topbar">
        <div>
          <h1 className="h1">${title}</h1>
          <div className="sub">${ui.subtitle} · ${biz.brand}</div>
        </div>
        <div className="pills">
          <div className="pill">
            <span className="dot" style=${{ background: dutyColor(online, onDuty) }}></span>
            <span><span className="strong">${biz.agent}</span> · ${(dutyLabel(ui, online, onDuty) || "").toLowerCase()}</span>
          </div>
        </div>
      </div>
      ${!online && html`<div className="downbanner">
        <${Icon} d="phone" size=${16} />${ui.agent_down_warning}
      </div>`}
      ${view === "inbox" && html`<${Inbox} items=${items} biz=${biz} ui=${ui}
        onHandled=${onHandled} onViewCall=${setOpenCall} onCancel=${onCancel} />`}
      ${view === "calls" && html`<${Calls} calls=${calls} ui=${ui} onOpen=${setOpenCall} />`}
      ${view === "overview" && html`<${Overview} stats=${stats} biz=${biz} ui=${ui} />`}
      ${view === "settings" && html`<${Settings} biz=${biz} ui=${ui}
        onSaved=${() => refresh().catch(() => {})} />
        <${Notifications} ui=${ui} />`}
    </main>
    <${TabBar} ui=${ui} view=${view} setView=${setView} newCount=${newCount} />
    ${openCall && html`<${Transcript} callId=${openCall} ui=${ui} onClose=${() => setOpenCall(null)} />`}
  </div>`;
}

ReactDOM.createRoot(document.getElementById("root")).render(html`<${App} />`);
