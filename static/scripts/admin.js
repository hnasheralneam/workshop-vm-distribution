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

let pollHandle = null;
let editingPool = null;

async function loadPool() {
   const res = await fetch("/api/admin/pool");
   if (!res.ok) return;
   const entries = await res.json();
   poolRows.innerHTML = "";
   for (const entry of entries) {
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
      poolRows.appendChild(tr);
   }
}

function setBusy(busy) {
   for (const btn of document.querySelectorAll("button")) {
      btn.disabled = busy;
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
      redeployRows.textContent = "No pools yet.";
      return;
   }
   for (const pool of pools) {
      const row = document.createElement("div");
      row.className = "row";
      const label = document.createElement("span");
      label.textContent = `${pool.name}${pool.config.dispenser ? " — dispenser" : ""}${pool.config.pool_code ? ` — code ${pool.config.pool_code}` : ""} (${pool.available} available)`;
      const input = document.createElement("input");
      input.type = "number";
      input.min = "1";
      input.value = pool.count;
      const copyBtn = document.createElement("button");
      copyBtn.textContent = "Copy link";
      copyBtn.addEventListener("click", () => {
         const code = pool.config.pool_code ? `?code=${encodeURIComponent(pool.config.pool_code)}` : "";
         copyToClipboard(`${location.origin}/claim/${encodeURIComponent(pool.name)}${code}`);
         copyBtn.textContent = "Copied!";
         setTimeout(() => { copyBtn.textContent = "Copy link"; }, 1500);
      });
      const codeBtn = document.createElement("button");
      codeBtn.className = "secondary";
      codeBtn.textContent = "Set code";
      codeBtn.addEventListener("click", async () => {
         const code = prompt(`Access code for pool "${pool.name}". Leave empty to clear:`, pool.config.pool_code || "");
         if (code === null) return;
         const res = await fetch("/api/admin/pool-code", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ pool: pool.name, code })
         });
         if (res.ok) loadPools();
      });
      const btn = document.createElement("button");
      btn.textContent = "Deploy";
      btn.addEventListener("click", () => {
         if (!confirm(`Deploy ${input.value} VM(s) to pool "${pool.name}"?`)) return;
         startJob("/api/admin/redeploy", { name: pool.name, count: parseInt(input.value, 10) });
      });
      const editBtn = document.createElement("button");
      editBtn.textContent = "Edit";
      editBtn.addEventListener("click", () => openDeployModal(pool));
      const deleteBtn = document.createElement("button");
      deleteBtn.className = "danger";
      deleteBtn.textContent = "Delete";
      deleteBtn.addEventListener("click", () => deletePool(pool));
      row.append(label, input, copyBtn, codeBtn, btn, editBtn, deleteBtn);
      redeployRows.appendChild(row);
   }
}

function openDeployModal(pool) {
   editingPool = pool;
   const passwordField = provisionForm.elements.template_vm_password;
   if (pool) {
      deployTitle.textContent = `Edit pool: ${pool.name}`;
      provisionForm.elements.pool_name.value = pool.name;
      provisionForm.elements.pool_name.disabled = true;
      provisionForm.elements.pool_code.value = pool.config.pool_code ?? "";
      provisionForm.elements.vm_count.value = pool.count;
      provisionForm.elements.vm_duration_hours.value = pool.config.guac_link_ttl_seconds / 3600;
      dispenserInput.checked = !!pool.config.dispenser;
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
      provisionForm.elements.pool_code.value = "";
      provisionForm.elements.vm_count.value = 5;
      provisionForm.elements.vm_duration_hours.value = 2;
      provisionForm.elements.template_vm_access_method.value = "ssh";
      provisionForm.elements.template_vm_username.value = "";
      provisionForm.elements.template_vm_id.value = "";
      dispenserInput.checked = true;
      passwordField.value = "";
      passwordField.placeholder = "";
      provisionBtn.textContent = "Provision";
   }
   deployDialog.showModal();
}

async function deletePool(pool) {
   const suffix = pool.available > 0
      ? ` Its ${pool.available} available VM(s) stay claimable — destroy them from the Destroy card.`
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
      jobStatus.textContent = `${job.kind} — ${job.status}`;
      jobLog.textContent = job.log.join("\n");
      jobLog.scrollTop = jobLog.scrollHeight;
      if (job.status !== "running") {
         clearInterval(pollHandle);
         setBusy(false);
         loadPool();
         loadPools();
      }
   }, 1500);
}

async function startJob(url, body) {
   setBusy(true);
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
