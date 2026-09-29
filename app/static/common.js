// Shared helpers for all pages.

async function api(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  let data = null;
  const text = await res.text();
  if (text) {
    try { data = JSON.parse(text); } catch { data = text; }
  }
  if (!res.ok) {
    const message =
      (data && data.detail && data.detail.message) ||
      (data && data.detail && typeof data.detail === "string") ||
      `请求失败（${res.status}）`;
    const code = data && data.detail && data.detail.code;
    const err = new Error(message);
    err.status = res.status;
    err.code = code;
    err.detail = data && data.detail;
    throw err;
  }
  return data;
}

function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("zh-CN", { hour12: false });
}

const STATE_LABEL = {
  missing: "待译",
  pending: "待审",
  stale: "过期",
  confirmed: "已确认",
};

function stateBadge(state) {
  return `<span class="badge badge-${state}">${STATE_LABEL[state] || state}</span>`;
}

function toast(message, kind = "") {
  let host = document.getElementById("toast-host");
  if (!host) {
    host = document.createElement("div");
    host.id = "toast-host";
    document.body.appendChild(host);
  }
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  host.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

async function loadMe() {
  try {
    return await api("GET", "/api/auth/me");
  } catch {
    return null;
  }
}

async function renderTopbar(active) {
  const me = await loadMe();
  const roleName = { curator: "馆员", translator: "译者", reviewer: "校对者" };
  const links = [
    ["/visitor", "游客页"],
  ];
  if (me) {
    links.push(["/workspace", "译审工作台"]);
    if (me.role === "curator") links.push(["/admin", "馆员后台"]);
  }
  const right = me
    ? `<span class="who"><b>${esc(me.display_name)}</b>（${roleName[me.role] || me.role}）</span>
       <a href="#" id="logout-link">退出</a>`
    : `<a href="/login">馆员 / 译者登录</a>`;
  document.body.insertAdjacentHTML(
    "afterbegin",
    `<nav class="topbar">
       <span class="brand">多语展签审校</span>
       ${links
         .map(([href, name]) =>
           `<a href="${href}"${href === active ? ' class="active"' : ""}>${name}</a>`)
         .join("")}
       <span class="spacer"></span>${right}
     </nav>`
  );
  const lo = document.getElementById("logout-link");
  if (lo) {
    lo.addEventListener("click", async (e) => {
      e.preventDefault();
      await api("POST", "/api/auth/logout");
      location.href = "/visitor";
    });
  }
  return me;
}
