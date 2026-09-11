const provisionForm = document.getElementById("provision-form");
const provisionBtn = document.getElementById("provision-btn");
const destroyAllBtn = document.getElementById("destroy-all-btn");
const destroyExpiredBtn = document.getElementById("destroy-expired-btn");
const destroySelectedBtn = document.getElementById("destroy-selected-btn");
const refreshPoolBtn = document.getElementById("refresh-pool-btn");
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

let pollHandle = null;
let runningJob = null;
let editingPool = null;
const collapsedPools = new Set();

const ICONS = {
   edit: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>',
   trash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>',
};

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
         const expiresLabel = entry.expires_at
            ? new Date(entry.expires_at * 1000).toLocaleString()
            : "n/a";
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
         expiresCell.textContent = expiresLabel;
         tr.appendChild(expiresCell);
         if (collapsedPools.has(pool)) tr.classList.add("hidden");
         poolRows.appendChild(tr);
      }
   }
}

function setBusy(busy, kind = "destroy") {
   runningJob = busy ? kind : null;
   for (const btn of document.querySelectorAll("button")) {
      btn.disabled = busy && (kind === "destroy" || btn.classList.contains("deploy-btn"));
   }
}

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

async function loadPools() {
   const res = await fetch("/api/admin/pools");
   if (!res.ok) return;
   const pools = await res.json();
   redeployRows.innerHTML = "";
   if (pools.length === 0) {
      redeployRows.textContent = "No pools.";
      return;
   }
   for (const pool of pools) {
      const card = document.createElement("div");
      card.className = "pool-card";
      card.dataset.pool = pool.name;
      const header = document.createElement("div");
      header.className = "pool-header";
      const name = document.createElement("h3");
      name.textContent = pool.name;
      const editBtn = document.createElement("button");
      editBtn.className = "icon-btn";
      editBtn.title = "Edit";
      editBtn.setAttribute("aria-label", "Edit");
      editBtn.innerHTML = ICONS.edit;
      editBtn.addEventListener("click", () => openDeployModal(pool));
      const deleteBtn = document.createElement("button");
      deleteBtn.className = "icon-btn danger";
      deleteBtn.title = "Delete";
      deleteBtn.setAttribute("aria-label", "Delete");
      deleteBtn.innerHTML = ICONS.trash;
      deleteBtn.addEventListener("click", () => deletePool(pool));
      header.append(name, editBtn, deleteBtn);
      const meta = document.createElement("p");
      meta.className = "pool-meta";
      meta.textContent = `${pool.available} available, ${pool.total} total`;
      card.append(header, meta);
      const badge = document.createElement("p");
      badge.className = "pool-badge";
      const badges = [pool.config.dispenser && "dispenser", pool.config.private && "private"].filter(Boolean).join(", ");
      badge.textContent = badges || "\u00A0";
      card.append(badge);
      const actions = document.createElement("div");
      actions.className = "pool-actions";
      const copyBtn = document.createElement("button");
      copyBtn.className = "secondary";
      copyBtn.textContent = "Copy link";
      copyBtn.addEventListener("click", () => {
         const code = pool.config.pool_code ? `?code=${encodeURIComponent(pool.config.pool_code)}` : "";
         copyToClipboard(`${location.origin}/claim/${encodeURIComponent(pool.name)}${code}`);
         copyBtn.textContent = "Copied!";
         setTimeout(() => { copyBtn.textContent = "Copy link"; }, 1500);
      });
      actions.append(copyBtn);
      if (pool.config.pool_code) {
         const codeBtn = document.createElement("button");
         codeBtn.className = "secondary";
         codeBtn.textContent = `Copy code: ${pool.config.pool_code}`;
         codeBtn.addEventListener("click", () => {
            copyToClipboard(pool.config.pool_code);
            codeBtn.textContent = "Copied!";
            setTimeout(() => { codeBtn.textContent = `Copy code: ${pool.config.pool_code}`; }, 1500);
         });
         actions.append(codeBtn);
      }
      const spacer = document.createElement("span");
      spacer.className = "spacer";
      const input = document.createElement("input");
      input.type = "number";
      input.min = "1";
      input.value = pool.count;
      const btn = document.createElement("button");
      btn.className = "deploy-btn";
      btn.disabled = runningJob !== null;
      btn.textContent = "Deploy";
      btn.addEventListener("click", () => {
         if (!confirm(`Deploy ${input.value} VM(s) to pool "${pool.name}"?`)) return;
         startJob("/api/admin/redeploy", { name: pool.name, count: parseInt(input.value, 10) });
      });
      actions.append(spacer, input, btn);
      card.append(actions);
      redeployRows.appendChild(card);
   }
}

async function updatePoolStats() {
   const res = await fetch("/api/admin/pools");
   if (!res.ok) return;
   const pools = await res.json();
   for (const pool of pools) {
      const card = redeployRows.querySelector(`[data-pool="${CSS.escape(pool.name)}"]`);
      if (!card) continue;
      card.querySelector(".pool-meta").textContent = `${pool.available} available, ${pool.total} total`;
      card.querySelector(".deploy-btn").disabled = runningJob !== null;
   }
}

function openDeployModal(pool) {
   editingPool = pool;
   const passwordField = provisionForm.elements.template_vm_password;
   passwordField.required = !pool;
   if (pool) {
      deployTitle.textContent = `Edit pool: ${pool.name}`;
      provisionForm.elements.pool_name.value = pool.name;
      provisionForm.elements.pool_name.disabled = true;
      provisionForm.elements.vm_count.value = pool.count;
      provisionForm.elements.vm_duration_hours.value = pool.config.guac_link_ttl_seconds / 3600;
      dispenserInput.checked = !!pool.config.dispenser;
      privateInput.checked = !!pool.config.private;
      provisionForm.elements.template_vm_access_method.value = pool.config.template_vm_access_method;
      provisionForm.elements.template_vm_id.value = pool.config.template_vm_id;
      provisionForm.elements.template_vm_username.value = pool.config.template_vm_username ?? "";
      passwordField.value = "";
      passwordField.placeholder = "(leave blank to keep saved)";
      provisionBtn.textContent = "Save";
   } else {
      deployTitle.textContent = "New deployment";
      provisionForm.elements.pool_name.disabled = false;
      provisionForm.elements.pool_name.value = "";
      provisionForm.elements.vm_count.value = 5;
      provisionForm.elements.vm_duration_hours.value = 2;
      provisionForm.elements.template_vm_access_method.value = "ssh";
      provisionForm.elements.template_vm_username.value = "";
      provisionForm.elements.template_vm_id.value = "";
      dispenserInput.checked = true;
      privateInput.checked = false;
      passwordField.value = "";
      passwordField.placeholder = "";
      provisionBtn.textContent = "Provision";
   }
   deployDialog.showModal();
}

async function deletePool(pool) {
   const suffix = pool.available > 0
      ? ` Its ${pool.available} available VMs are not removed`
      : "";
   if (!confirm(`Delete saved config for pool "${pool.name}"?${suffix}`)) return;
   const res = await fetch(`/api/admin/pools/${encodeURIComponent(pool.name)}`, { method: "DELETE" });
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
      const res = await fetch(`/api/admin/pools/${encodeURIComponent(editingPool.name)}`, {
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

destroyAllBtn.addEventListener("click", () => {
   if (!confirm("Destroy ALL workshop VMs?")) return;
   startJob("/api/admin/destroy", { mode: "all" });
});

destroyExpiredBtn.addEventListener("click", () => {
   if (!confirm("Destroy all EXPIRED workshop VMs?")) return;
   startJob("/api/admin/destroy", { mode: "expired" });
});

destroySelectedBtn.addEventListener("click", () => {
   const vmids = [...document.querySelectorAll(".vm-checkbox:checked")].map((el) => parseInt(el.value, 10));
   if (vmids.length === 0) {
      alert("Select at least one VM.");
      return;
   }
   if (!confirm(`Destroy ${vmids.length} selected VM(s)?`)) return;
   startJob("/api/admin/destroy", { mode: "specific", vmids });
});

refreshPoolBtn.addEventListener("click", loadPool);

loadPool();
loadPools();
setInterval(() => {
   loadPool();
   updatePoolStats();
}, 10000);
