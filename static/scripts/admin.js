const provisionForm = document.getElementById("provision-form");
const provisionBtn = document.getElementById("provision-btn");
const destroyAllBtn = document.getElementById("destroy-all-btn");
const destroySelectedBtn = document.getElementById("destroy-selected-btn");
const extendSelectedBtn = document.getElementById("extend-selected-btn");
const poolRows = document.getElementById("pool-rows");
const redeployRows = document.getElementById("redeploy-rows");
const jobStatus = document.getElementById("job-status");
const jobLog = document.getElementById("job-log");
const deployDialog = document.getElementById("deploy-dialog");
const deployTitle = document.getElementById("deploy-title");
const newDeployBtn = document.getElementById("new-deploy-btn");
const deployCancelBtn = document.getElementById("deploy-cancel-btn");
const dispenserInput = document.getElementById("dispenser");
const privateInput = document.getElementById("private");
const deployCode = document.getElementById("deploy-code");
const deployHint = document.getElementById("deploy-hint");
const groupSeg = document.querySelector('.segmented[data-input="group"]');
const accessSeg = document.querySelector('.segmented[data-input="template_vm_access_method"]');
const poolSearch = document.getElementById("pool-search");
const poolEmpty = document.getElementById("pool-empty");
const logsBtn = document.getElementById("logs-btn");
const logsDialog = document.getElementById("logs-dialog");
const logsRefreshBtn = document.getElementById("logs-refresh-btn");
const logsCloseBtn = document.getElementById("logs-close-btn");
const logSearch = document.getElementById("log-search");
const serverLog = document.getElementById("server-log");
const logCount = document.getElementById("log-count");

let pollHandle = null;
let runningJob = null;
let editingPool = null;
let logLines = [];
const collapsedPools = new Set();

const ICONS = {
   edit: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>',
   trash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>',
   link: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>',
   lock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>',
   lockOpen: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/></svg>',
   dispense: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/><path d="M15 11v6"/><path d="M12 14h6"/></svg>',
   check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>',
};

function formatTimeAmount(timeMs, units = "yMwdhms", decimalPrecision) {
   let seconds = timeMs / 1000;
   let minutes = seconds / 60;
   let hours = minutes / 60;
   let days = hours / 24;
   let weeks = days / 7;
   let months = weeks / 4.34524;  // Average number of weeks per month
   let years = months / 12;

   let result = "";

   if (years >= 1 && units.includes("y")) {
      result += Math.floor(years) + " year" + (Math.floor(years) !== 1 ? "s " : " ");
   }
   months %= 12;
   if (months >= 1 && units.includes("M")) {
      result += Math.floor(months) + " month" + (Math.floor(months) !== 1 ? "s " : " ");
   }
   weeks %= 4.34524;
   if (weeks >= 1 && units.includes("w")) {
      result += Math.floor(weeks) + " week" + (Math.floor(weeks) !== 1 ? "s " : " ");
   }
   days %= 7;
   if (days >= 1 && units.includes("d")) {
      result += Math.floor(days) + " day" + (Math.floor(days) !== 1 ? "s " : " ");
   }
   hours %= 24;
   if (hours >= 1 && units.includes("h")) {
      result += Math.floor(hours) + " hour" + (Math.floor(hours) !== 1 ? "s " : " ");
   }
   minutes %= 60;
   if (minutes >= 1 && units.includes("m")) {
      result += Math.floor(minutes) + " minute" + (Math.floor(minutes) !== 1 ? "s and " : " and ");
   }
   seconds = timeMs % 60000 / 1000;
   if (units.includes("s")) {
      result += seconds.toFixed(decimalPrecision || 0) + " second" + (seconds !== 1 ? "s" : "");
   }

   return result;
}

async function loadPool() {
   const res = await fetch("/api/admin/pool");
   if (!res.ok) return;
   const entries = await res.json();
   const checked = new Set([...poolRows.querySelectorAll("input:checked")].map((el) => el.value));
   poolRows.innerHTML = "";
   const groups = new Map();
   for (const entry of entries) {
      if (!groups.has(entry.pool)) groups.set(entry.pool, []);
      groups.get(entry.pool).push(entry);
   }
   for (const [pool, groupEntries] of groups) {
      const head = document.createElement("tr");
      head.className = "pool-group";
      const cell = document.createElement("td");
      cell.colSpan = 6;
      cell.textContent = `${collapsedPools.has(pool) ? "▸" : "▾"} ${pool ?? "(no pool)"} (${groupEntries.length})`;
      head.appendChild(cell);
      head.addEventListener("click", () => {
         if (collapsedPools.has(pool)) {
            collapsedPools.delete(pool);
         } else {
            collapsedPools.add(pool);
         }
         loadPool();
      });
      poolRows.appendChild(head);
      for (const entry of groupEntries) {
         const statusLabel = entry.expired ? "expired" : (entry.claimed ? "claimed" : "available");
         const tr = document.createElement("tr");
         const checkCell = document.createElement("td");
         const checkbox = document.createElement("input");
         checkbox.type = "checkbox";
         checkbox.className = "vm-checkbox";
         checkbox.value = entry.vmid;
         checkbox.checked = checked.has(checkbox.value);
         checkCell.appendChild(checkbox);
         tr.appendChild(checkCell);
         for (const value of [entry.vmid, entry.pool ?? "", entry.student_id ?? ""]) {
            const td = document.createElement("td");
            td.textContent = value;
            tr.appendChild(td);
         }
         const statusCell = document.createElement("td");
         const pill = document.createElement("span");
         pill.className = `status-pill status-${statusLabel}`;
         pill.textContent = statusLabel;
         statusCell.appendChild(pill);
         tr.appendChild(statusCell);
         const expiresCell = document.createElement("td");
         if (entry.expires_at) {
            expiresCell.title = new Date(entry.expires_at * 1000).toLocaleString();
            expiresCell.textContent = entry.expired
               ? "expired"
               : formatTimeAmount(Math.max(0, entry.expires_at * 1000 - Date.now()), "dhm").replace(/ and $/, "").trim() || "under a minute";
         } else {
            expiresCell.textContent = "n/a";
         }
         tr.appendChild(expiresCell);
         if (collapsedPools.has(pool)) tr.classList.add("hidden");
         poolRows.appendChild(tr);
      }
   }
   updateSelectionButtons();
}

function setBusy(busy, kind = "destroy") {
   runningJob = busy ? kind : null;
   for (const btn of document.querySelectorAll("button")) {
      btn.disabled = busy && (kind === "destroy" || btn.classList.contains("deploy-btn"));
   }
   if (!busy) updateSelectionButtons();
}

function updateSelectionButtons() {
   const any = poolRows.querySelector(".vm-checkbox:checked") !== null;
   extendSelectedBtn.disabled = runningJob === "destroy" || !any;
   destroySelectedBtn.disabled = runningJob === "destroy" || !any;
}

poolRows.addEventListener("change", (e) => {
   if (e.target.classList.contains("vm-checkbox")) updateSelectionButtons();
});

function copyToClipboard(text) {
   if (navigator.clipboard) {
      navigator.clipboard.writeText(text).catch(() => fallbackCopy(text));
      return;
   }
   fallbackCopy(text);
}

function fallbackCopy(text) {
   const textarea = document.createElement("textarea");
   textarea.value = text;
   document.body.appendChild(textarea);
   textarea.select();
   document.execCommand("copy");
   textarea.remove();
}

let poolList = [];
let poolFilter = "all";
const POOL_GROUPS = ["Workshop", "Challenge", "Generic"];
let activeGroup = "All";
let priorGroup = "All";
const groupOf = (p) => (POOL_GROUPS.includes(p.config.group) ? p.config.group : "Generic");
const poolRecency = (p) => Math.max(p.last_used || 0, p.config.modified_at || 0);

const POOL_FILTERS = {
   all: () => true,
   "in-use": (p) => p.in_use > 0,
   free: (p) => p.available > 0,
   dispenser: (p) => !!p.config.dispenser,
   private: (p) => !!p.config.private,
};

async function loadPools() {
   const res = await fetch("/api/admin/pools");
   if (!res.ok) return;
   poolList = await res.json();
   poolList.sort((a, b) => poolRecency(b) - poolRecency(a) || a.name.localeCompare(b.name));
   renderPools();
}

function buildPoolCard(pool) {
      const card = document.createElement("div");
      card.className = "pool-card";
      card.dataset.pool = pool.code;
      card.dataset.name = pool.name.toLowerCase();
      card.dataset.available = pool.available;
      card.dataset.total = pool.total;
      card.dataset.inUse = pool.in_use;
      card.dataset.dispenser = pool.config.dispenser ? "1" : "0";
      card.dataset.private = pool.config.private ? "1" : "0";
      card.dataset.group = groupOf(pool);
      const name = document.createElement("div");
      name.className = "pool-name";
      const nameText = document.createElement("h3");
      nameText.textContent = pool.name;
      name.append(nameText);
      if (pool.config.dispenser) {
         const flag = document.createElement("span");
         flag.className = "pool-flag";
         flag.dataset.tip = "Dispenser: clones a VM when none are free";
         flag.innerHTML = ICONS.dispense;
         name.append(flag);
      }
      const code = document.createElement("span");
      code.className = "pool-code";
      code.dataset.tip = "Copy claim code";
      if (pool.config.pool_code) {
         code.textContent = pool.config.pool_code;
         code.setAttribute("role", "button");
         code.tabIndex = 0;
         const copy = () => {
            copyToClipboard(pool.config.pool_code);
            code.textContent = "Copied!";
            setTimeout(() => { code.textContent = pool.config.pool_code; }, 1500);
         };
         code.addEventListener("click", copy);
         code.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") {
               e.preventDefault();
               copy();
            }
         });
      }
      const os = document.createElement("span");
      os.className = "pool-os";
      const rdp = pool.config.template_vm_access_method === "rdp";
      os.textContent = rdp ? "Windows" : "Linux";
      const type = document.createElement("span");
      type.className = "pool-type";
      type.textContent = groupOf(pool);
      const inUse = document.createElement("span");
      inUse.className = "pool-count";
      inUse.dataset.tip = `${pool.available} free of ${pool.total}`;
      inUse.setAttribute("aria-label", `${pool.available} free of ${pool.total}`);
      inUse.textContent = `${pool.in_use} / ${pool.total}`;
      const actions = document.createElement("div");
      actions.className = "pool-actions";
      const linkBtn = document.createElement("button");
      linkBtn.type = "button";
      linkBtn.className = "icon-btn";
      linkBtn.dataset.tip = "Copy claim link";
      linkBtn.setAttribute("aria-label", "Copy claim link");
      linkBtn.innerHTML = ICONS.link;
      linkBtn.addEventListener("click", () => {
         copyToClipboard(`${location.origin}/claim?code=${encodeURIComponent(pool.code)}`);
         linkBtn.innerHTML = ICONS.check;
         setTimeout(() => { linkBtn.innerHTML = ICONS.link; }, 1500);
      });
      const editBtn = document.createElement("button");
      editBtn.type = "button";
      editBtn.className = "icon-btn";
      editBtn.dataset.tip = "Edit pool";
      editBtn.setAttribute("aria-label", "Edit pool");
      editBtn.innerHTML = ICONS.edit;
      editBtn.addEventListener("click", () => openDeployModal(pool));
      const lockBtn = document.createElement("button");
      lockBtn.type = "button";
      lockBtn.className = "icon-btn lock-btn";
      const syncLock = () => {
         lockBtn.innerHTML = pool.config.private ? ICONS.lock : ICONS.lockOpen;
         const tip = pool.config.private ? "Private: click to toggle" : "Public: click to toggle";
         lockBtn.dataset.tip = tip;
         lockBtn.setAttribute("aria-label", tip);
      };
      syncLock();
      lockBtn.addEventListener("click", async () => {
         const res = await fetch(`/api/admin/pools/${encodeURIComponent(pool.code)}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ private: !pool.config.private }),
         });
         if (res.ok) {
            pool.config.private = !pool.config.private;
            syncLock();
            lockBtn.closest(".pool-card").dataset.private = pool.config.private ? "1" : "0";
         } else {
            const data = await res.json().catch(() => ({}));
            alert(data.detail || "Failed to update pool.");
         }
      });
      const deleteBtn = document.createElement("button");
      deleteBtn.type = "button";
      deleteBtn.className = "icon-btn danger";
      deleteBtn.dataset.tip = "Delete pool";
      deleteBtn.setAttribute("aria-label", "Delete pool");
      deleteBtn.innerHTML = ICONS.trash;
      deleteBtn.addEventListener("click", () => deletePool(pool));
      const input = document.createElement("input");
      input.type = "number";
      input.min = "1";
      input.value = pool.count;
      const btn = document.createElement("button");
      btn.className = "deploy-btn";
      btn.disabled = runningJob !== null;
      btn.textContent = "Deploy";
      if (pool.config.template_vm_access_method === "rdp") btn.classList.add("win");
      btn.addEventListener("click", () => {
         if (!confirm(`Deploy ${input.value} VM(s) to pool "${pool.name}"?`)) return;
         startJob("/api/admin/redeploy", { code: pool.code, count: parseInt(input.value, 10) });
      });
      const deployGroup = document.createElement("div");
      deployGroup.className = "deploy-group";
      deployGroup.append(btn, input);
      card.append(name, code, os, type, inUse, actions, deployGroup);
      actions.append(linkBtn, editBtn, lockBtn, deleteBtn);
      return card;
}

function updateTabCounts() {
   for (const tab of document.querySelectorAll("#pool-tabs .pool-tab")) {
      const g = tab.dataset.group;
      tab.querySelector(".pool-tab-count").textContent = g === "All" ? poolList.length : poolList.filter((p) => groupOf(p) === g).length;
   }
}

function buildPoolHeader() {
   const head = document.createElement("div");
   head.className = "pool-list-header";
   for (const label of ["Name", "Code", "OS", "Type", "In use", "Actions", ""]) {
      const cell = document.createElement("span");
      cell.textContent = label;
      if (label === "Type") cell.className = "pool-type";
      head.append(cell);
   }
   return head;
}

function renderPools() {
   updateTabCounts();
   redeployRows.innerHTML = "";
   if (poolList.length === 0) {
      redeployRows.classList.remove("has-rows");
      redeployRows.textContent = "No pools.";
      poolEmpty.hidden = true;
      return;
   }
   redeployRows.classList.add("has-rows");
   redeployRows.append(buildPoolHeader());
   for (const pool of poolList) redeployRows.appendChild(buildPoolCard(pool));
   applyPoolView();
}

function applyPoolView() {
   redeployRows.classList.toggle("view-all", activeGroup === "All");
   const q = poolSearch.value.trim().toLowerCase();
   let visible = 0;
   for (const card of redeployRows.querySelectorAll(".pool-card")) {
      const pool = {
         available: parseInt(card.dataset.available, 10) || 0,
         in_use: parseInt(card.dataset.inUse, 10) || 0,
         config: { dispenser: card.dataset.dispenser === "1", private: card.dataset.private === "1" },
      };
      const ok = (activeGroup === "All" || card.dataset.group === activeGroup) && (!q || card.dataset.name.includes(q)) && POOL_FILTERS[poolFilter](pool);
      card.classList.toggle("hidden", !ok);
      if (ok) visible++;
   }
   if (visible > 0) {
      poolEmpty.hidden = true;
   } else {
      const anyInTab = activeGroup === "All" || [...redeployRows.querySelectorAll(".pool-card")].some((c) => c.dataset.group === activeGroup);
      poolEmpty.textContent = anyInTab ? "No pools match." : "No pools here yet.";
      poolEmpty.hidden = false;
   }
   const head = redeployRows.querySelector(".pool-list-header");
   if (head) head.classList.toggle("hidden", visible === 0);
}

function setGroup(group) {
   activeGroup = group;
   if (group !== "All") priorGroup = group;
   for (const tab of document.querySelectorAll("#pool-tabs .pool-tab")) tab.classList.toggle("active", tab.dataset.group === activeGroup);
   applyPoolView();
}

poolSearch.addEventListener("input", () => {
   if (poolSearch.value.trim()) {
      if (activeGroup !== "All") setGroup("All");
      else applyPoolView();
   } else if (activeGroup === "All" && priorGroup !== "All") {
      setGroup(priorGroup);
   } else {
      applyPoolView();
   }
});
for (const tab of document.querySelectorAll("#pool-tabs .pool-tab")) {
   tab.addEventListener("click", () => setGroup(tab.dataset.group));
}
for (const chip of document.querySelectorAll("#pool-filters .filter-chip")) {
   chip.addEventListener("click", () => {
      poolFilter = chip.dataset.filter;
      for (const c of document.querySelectorAll("#pool-filters .filter-chip")) c.classList.toggle("active", c === chip);
      applyPoolView();
   });
}
async function updatePoolStats() {
   const res = await fetch("/api/admin/pools");
   if (!res.ok) return;
   const pools = await res.json();
   for (const pool of pools) {
      const card = redeployRows.querySelector(`[data-pool="${CSS.escape(pool.code)}"]`);
      if (!card) continue;
      const count = card.querySelector(".pool-count");
      count.textContent = `${pool.in_use} / ${pool.total}`;
      count.dataset.tip = `${pool.available} free of ${pool.total}`;
      count.setAttribute("aria-label", `${pool.available} free of ${pool.total}`);
      card.dataset.available = pool.available;
      card.dataset.total = pool.total;
      card.dataset.inUse = pool.in_use;
      card.querySelector(".deploy-btn").disabled = runningJob !== null;
   }
   applyPoolView();
}

function setSegment(seg, value) {
   provisionForm.elements[seg.dataset.input].value = value;
   for (const btn of seg.children) btn.classList.toggle("active", btn.dataset.value === value);
}

function openDeployModal(pool) {
   editingPool = pool;
   const passwordField = provisionForm.elements.template_vm_password;
   passwordField.required = !pool;
   if (pool) {
      deployTitle.textContent = `Edit pool: ${pool.name}`;
      deployCode.textContent = pool.code;
      deployCode.dataset.code = pool.code;
      deployCode.hidden = false;
      deployHint.textContent = "Saves the pool config. Running VMs are unchanged.";
      provisionForm.elements.pool_name.value = pool.name;
      provisionForm.elements.vm_count.value = pool.count;
      provisionForm.elements.vm_duration_hours.value = parseFloat((pool.config.guac_link_ttl_seconds / 3600).toFixed(2));
      dispenserInput.checked = !!pool.config.dispenser;
      privateInput.checked = !!pool.config.private;
      setSegment(groupSeg, groupOf(pool));
      setSegment(accessSeg, pool.config.template_vm_access_method);
      provisionForm.elements.template_vm_id.value = pool.config.template_vm_id;
      provisionForm.elements.template_vm_username.value = pool.config.template_vm_username ?? "";
      passwordField.value = "";
      passwordField.placeholder = "(leave blank to keep saved)";
      provisionBtn.textContent = "Save";
   } else {
      deployTitle.textContent = "New pool";
      deployCode.hidden = true;
      deployHint.textContent = "Provisioning clones VMs from the template immediately after saving.";
      provisionForm.elements.pool_name.value = "";
      provisionForm.elements.vm_count.value = 5;
      provisionForm.elements.vm_duration_hours.value = 2;
      provisionForm.elements.template_vm_username.value = "";
      provisionForm.elements.template_vm_id.value = "";
      dispenserInput.checked = true;
      privateInput.checked = false;
      setSegment(groupSeg, "Generic");
      setSegment(accessSeg, "ssh");
      passwordField.value = "";
      passwordField.placeholder = "";
      provisionBtn.textContent = "Provision";
   }
   deployDialog.showModal();
   deployDialog.querySelector(".dialog-body").scrollTop = 0;
}

extendSelectedBtn.addEventListener("click", async () => {
   const vmids = [...document.querySelectorAll(".vm-checkbox:checked")].map((el) => parseInt(el.value, 10));
   if (!confirm(`Extend ${vmids.length} selected VM(s) by 1 hour?`)) return;
   for (const vmid of vmids) {
      const res = await fetch("/api/admin/extend", {
         method: "POST",
         headers: { "Content-Type": "application/json" },
         body: JSON.stringify({ vmid, hours: 1 }),
      });
      if (!res.ok) {
         const data = await res.json();
         alert(data.detail || "Failed to extend VM.");
         break;
      }
   }
   loadPool();
});

async function deletePool(pool) {
   const suffix = pool.available > 0
      ? ` Its ${pool.available} available VMs are not removed`
      : "";
   if (!confirm(`Delete saved config for pool "${pool.name}"?${suffix}`)) return;
   const res = await fetch(`/api/admin/pools/${encodeURIComponent(pool.code)}`, { method: "DELETE" });
   if (res.ok) loadPools();
}

async function pollJob(jobId) {
   if (pollHandle) clearInterval(pollHandle);
   let failures = 0;
   const finish = () => {
      clearInterval(pollHandle);
      pollHandle = null;
      jobStatus.textContent = "Job no longer running.";
      document.querySelector(".job-status").style.display = "none";
      setBusy(false);
      loadPool();
      loadPools();
   };
   pollHandle = setInterval(async () => {
      let res;
      try {
         res = await fetch(`/api/admin/job/${jobId}`);
      } catch (error) {
         if (++failures >= 5) finish();
         return;
      }
      if (res.status === 404) {
         finish();
         return;
      }
      if (!res.ok) {
         if (++failures >= 5) finish();
         return;
      }
      failures = 0;
      const job = await res.json();
      jobStatus.textContent = `${job.kind}: ${job.status}`;
      jobLog.textContent = job.log.join("\n");
      jobLog.scrollTop = jobLog.scrollHeight;
      if (job.status !== "running") {
         clearInterval(pollHandle);
         pollHandle = null;
         document.querySelector(".job-status").style.display = "none";
         setBusy(false);
         loadPool();
         loadPools();
      }
   }, 1500);
}

async function startJob(url, body) {
   setBusy(true, url.includes("destroy") ? "destroy" : "provision");
   document.querySelector(".job-status").style.display = "block";
   jobStatus.textContent = "Starting...";
   jobLog.textContent = "";
   const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
   });
   const data = await res.json();
   if (!res.ok) {
      jobStatus.textContent = data.detail || "Failed to start job.";
      setBusy(false);
      return;
   }
   pollJob(data.job_id);
}

provisionForm.addEventListener("submit", async (e) => {
   e.preventDefault();
   const formData = new FormData(provisionForm);
   const body = {};
   for (const [key, value] of formData.entries()) {
      if (key === "vm_duration_hours") continue;
      if (value !== "") body[key] = value;
   }

   const durationHours = formData.get("vm_duration_hours");
   if (durationHours !== null && durationHours !== "") {
      const hours = parseFloat(durationHours);
      if (!Number.isNaN(hours) && hours > 0) {
         body.guac_link_ttl_seconds = Math.round(hours * 3600);
      }
   }
   body.dispenser = dispenserInput.checked;
   body.private = privateInput.checked;

   if (editingPool) {
      const res = await fetch(`/api/admin/pools/${encodeURIComponent(editingPool.code)}`, {
         method: "PUT",
         headers: { "Content-Type": "application/json" },
         body: JSON.stringify(body),
      });
      if (!res.ok) {
         const data = await res.json();
         alert(data.detail || "Failed to update pool.");
         return;
      }
      deployDialog.close();
      loadPools();
      return;
   }

   deployDialog.close();
   startJob("/api/admin/provision", body);
});

newDeployBtn.addEventListener("click", () => openDeployModal(null));
deployCancelBtn.addEventListener("click", () => deployDialog.close());
deployDialog.addEventListener("click", (e) => {
   if (e.target === deployDialog) deployDialog.close();
});

for (const seg of [groupSeg, accessSeg]) {
   seg.addEventListener("click", (e) => {
      const btn = e.target.closest(".seg-btn");
      if (btn) setSegment(seg, btn.dataset.value);
   });
}

function copyDeployCode() {
   if (!deployCode.dataset.code) return;
   copyToClipboard(deployCode.dataset.code);
   deployCode.textContent = "Copied!";
   setTimeout(() => { deployCode.textContent = deployCode.dataset.code; }, 1500);
}

deployCode.addEventListener("click", copyDeployCode);
deployCode.addEventListener("keydown", (e) => {
   if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      copyDeployCode();
   }
});

destroyAllBtn.addEventListener("click", () => {
   if (!confirm("Destroy ALL workshop VMs?")) return;
   startJob("/api/admin/destroy", { mode: "all" });
});

destroySelectedBtn.addEventListener("click", () => {
   const vmids = [...document.querySelectorAll(".vm-checkbox:checked")].map((el) => parseInt(el.value, 10));
   if (!confirm(`Destroy ${vmids.length} selected VM(s)?`)) return;
   startJob("/api/admin/destroy", { mode: "specific", vmids });
});

async function reattachJob() {
   try {
      const res = await fetch("/api/admin/job");
      if (!res.ok) return;
      const job = await res.json();
      if (job.status !== "running") return;
      setBusy(true, job.kind);
      document.querySelector(".job-status").style.display = "block";
      jobStatus.textContent = `${job.kind}: ${job.status}`;
      jobLog.textContent = job.log.join("\n");
      jobLog.scrollTop = jobLog.scrollHeight;
      pollJob(job.id);
   } catch (error) {}
}

function renderLogs() {
   const query = logSearch.value.trim().toLowerCase();
   const matches = query ? logLines.filter((line) => line.toLowerCase().includes(query)) : logLines;
   serverLog.textContent = matches.join("\n");
   serverLog.scrollTop = serverLog.scrollHeight;
   logCount.textContent = query ? `${matches.length} of ${logLines.length} lines` : `${logLines.length} lines`;
}

async function loadLogs() {
   logCount.textContent = "Loading...";
   try {
      const res = await fetch("/api/admin/logs");
      if (!res.ok) throw new Error();
      logLines = (await res.json()).lines;
      renderLogs();
   } catch (error) {
      logCount.textContent = "Failed to load logs.";
   }
}

logsBtn.addEventListener("click", () => {
   logsDialog.showModal();
   serverLog.textContent = "";
   logSearch.value = "";
   loadLogs();
});
logsRefreshBtn.addEventListener("click", loadLogs);
logsCloseBtn.addEventListener("click", () => logsDialog.close());
logsDialog.addEventListener("click", (e) => {
   if (e.target === logsDialog) logsDialog.close();
});
logSearch.addEventListener("input", renderLogs);

loadPool();
loadPools();
reattachJob();
setInterval(() => {
   loadPool();
   updatePoolStats();
}, 10000);
