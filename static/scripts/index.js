const MAX_VMS = 2;
const PROVISION_TIMEOUT_MS = 600000;
const buttonsContainer = document.getElementById("claim-buttons");
const status = document.getElementById("status-message");
const vmsHeading = document.getElementById("vms-heading");
const vmsContainer = document.getElementById("vms");
const codeBtn = document.getElementById("code-btn");
const codeDialog = document.getElementById("code-dialog");
const codeForm = document.getElementById("code-form");
const codeInput = document.getElementById("code-input");
const codeError = document.getElementById("code-error");
const codeSubmitBtn = document.getElementById("code-submit-btn");
const codeCancelBtn = document.getElementById("code-cancel-btn");
const codeProgress = document.getElementById("code-progress");
const codeStage = document.getElementById("code-stage");
const claimProgress = document.getElementById("claim-progress");
const swapDialog = document.getElementById("swap-dialog");
const swapOptions = document.getElementById("swap-options");
const swapError = document.getElementById("swap-error");
const swapCancelBtn = document.getElementById("swap-cancel-btn");
const polls = new Map();
let pendingClaim = null;
let dialogTicket = null;
let vms = loadVms();

function loadVms() {
	const raw = localStorage.getItem("assigned_vm_urls");
	if (raw) {
		try {
			const parsed = JSON.parse(raw);
			if (Array.isArray(parsed)) return parsed;
		} catch (error) {}
	}
	const legacy = localStorage.getItem("assigned_vm_url");
	localStorage.removeItem("assigned_vm_url");
	const migrated = legacy ? [{ url: legacy, pool: null }] : [];
	if (migrated.length) localStorage.setItem("assigned_vm_urls", JSON.stringify(migrated));
	return migrated;
}

function saveVms() {
	localStorage.setItem("assigned_vm_urls", JSON.stringify(vms));
}

function addVm(url, pool, expiresAt) {
	vms.push({ url, pool: pool || null, expires_at: expiresAt ?? null });
	saveVms();
	renderVms();
}

function removeVm(vm) {
	const index = vms.indexOf(vm);
	if (index !== -1) vms.splice(index, 1);
	saveVms();
}

function atCap() {
	return vms.length >= MAX_VMS;
}

function loadTickets() {
	try {
		const parsed = JSON.parse(localStorage.getItem("pending_tickets"));
		if (Array.isArray(parsed)) return parsed;
	} catch (error) {}
	return [];
}

function saveTickets(tickets) {
	localStorage.setItem("pending_tickets", JSON.stringify(tickets));
}

function addPending(ticket, pool) {
	const tickets = loadTickets();
	const existing = tickets.find((t) => t.ticket === ticket);
	if (existing) {
		existing.pool = pool || existing.pool;
		saveTickets(tickets);
		return existing;
	}
	const pending = { ticket, pool: pool || null, started: Date.now() };
	tickets.push(pending);
	saveTickets(tickets);
	return pending;
}

function dropPending(ticket) {
	saveTickets(loadTickets().filter((t) => t.ticket !== ticket));
}

function setStatus(text, kind) {
	status.style.display = "block";
	status.classList.remove("error", "success");
	if (kind) status.classList.add(kind);
	status.innerText = text;
}

function claimLabelFromPath() {
	const match = location.pathname.match(/^\/claim\/(.+)$/);
	if (!match) return null;
	try {
		return decodeURIComponent(match[1]);
	} catch (error) {
		return match[1];
	}
}
const claimLabel = claimLabelFromPath();
const claimCode = new URLSearchParams(location.search).get("code");

function renderButtons(pools) {
	buttonsContainer.innerHTML = "";
	for (const pool of pools) {
		const btn = document.createElement("button");
		btn.className = "claim-btn";
		btn.innerText = pool.text;
		btn.onclick = () => handleTerminalAccess(pool.name, btn);
		buttonsContainer.appendChild(btn);
	}
}

function setButtonsDisabled(disabled) {
	for (const btn of buttonsContainer.querySelectorAll("button")) {
		btn.disabled = disabled;
	}
}

async function loadPoolButtons() {
	try {
		const response = await fetch("/api/types");
		const data = await response.json();
		codeBtn.hidden = !(data.coded > 0);
		const pools = (data.pools || []).filter((p) => !p.private && (p.dispenser || p.available > 0));
		if (pools.length > 1) {
			renderButtons(pools.map((p) => ({ text: `Claim ${p.name}`, name: p.name })));
		} else if (pools.length === 1) {
			renderButtons([{ text: "Claim", name: pools[0].name }]);
		} else {
			buttonsContainer.innerHTML = "";
			setStatus("No VMs are available right now. Please contact your instructor.");
		}
	} catch (error) {
		renderButtons([{ text: "Claim", name: null }]);
		setStatus("Could not load the pool list. You can still try claiming below.", "error");
	}
}

function expiryText(expiresAt) {
	const minutes = Math.round((expiresAt * 1000 - Date.now()) / 60000);
	if (minutes <= 0) return "expired";
	if (minutes < 60) return `${minutes}m left`;
	if (minutes < 1440) return `${Math.floor(minutes / 60)}h ${minutes % 60}m left`;
	return `${Math.floor(minutes / 1440)}d left`;
}

function renderVms() {
	vmsHeading.hidden = vms.length === 0;
	vmsContainer.innerHTML = "";
	for (const vm of vms) {
		const row = document.createElement("div");
		row.className = "vm-row";
		const label = document.createElement("span");
		label.className = "vm-label";
		label.innerText = vm.pool || "Workshop VM";
		if (vm.expires_at) {
			const expiry = document.createElement("span");
			expiry.className = "vm-expiry";
			expiry.innerText = expiryText(vm.expires_at);
			expiry.title = new Date(vm.expires_at * 1000).toLocaleString();
			label.appendChild(expiry);
		}
		const actions = document.createElement("div");
		actions.className = "vm-actions";
		const reconnectBtn = document.createElement("button");
		reconnectBtn.innerText = "Reconnect";
		reconnectBtn.onclick = () => reconnectVm(vm, row);
		const releaseBtn = document.createElement("button");
		releaseBtn.innerText = "Release";
		releaseBtn.className = "danger";
		releaseBtn.onclick = () => releaseVm(vm, row);
		actions.append(reconnectBtn, releaseBtn);
		row.append(label, actions);
		vmsContainer.appendChild(row);
	}
}

function setRowDisabled(row, disabled) {
	for (const btn of row.querySelectorAll("button")) btn.disabled = disabled;
}

async function validateVm(vm) {
	try {
		const response = await fetch("/api/validate", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ url: vm.url })
		});
		const data = await response.json();
		if (data.valid) {
			if (data.expires_at) {
				vm.expires_at = data.expires_at;
				saveVms();
			}
			return;
		}
		if (data.url) {
			vm.url = data.url;
			vm.expires_at = data.expires_at ?? null;
			saveVms();
			setStatus("An expired machine was replaced with a new one.");
		} else {
			removeVm(vm);
			setStatus("A machine is no longer available. Claim a new one below.");
		}
	} catch (error) {}
}

async function reconnectVm(vm, row) {
	setRowDisabled(row, true);
	setStatus("Reconnecting...");
	try {
		const response = await fetch("/api/reconnect", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ url: vm.url })
		});
		const data = await response.json();
		if (response.ok && data.url) {
			vm.url = data.url;
			if (data.expires_at) vm.expires_at = data.expires_at;
			saveVms();
			setStatus("Redirecting...");
			window.location.href = data.url;
		} else if (data.expired) {
			removeVm(vm);
			renderVms();
			setStatus("Your machine expired. Claim a new one below.");
			if (!atCap()) await loadPoolButtons();
		} else {
			setStatus(data.detail || "Reconnect failed. Please try again.", "error");
			setRowDisabled(row, false);
		}
	} catch (error) {
		setStatus("Network error. Please try again.", "error");
		setRowDisabled(row, false);
	}
}

async function releaseVm(vm, row) {
	if (!confirm(`Release ${vm.pool || "this machine"}? It will be immediately and permanently deleted.`)) {
		setRowDisabled(row, false);
		return;
	}
	setRowDisabled(row, true);
	try {
		const response = await fetch("/api/release", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ url: vm.url })
		});
		if (response.ok || response.status === 404) {
			removeVm(vm);
			renderVms();
			setStatus("Machine released.", "success");
		} else {
			const data = await response.json();
			setStatus(data.detail || "Release failed. Please try again.", "error");
			setRowDisabled(row, false);
			return;
		}
	} catch (error) {
		setStatus("Network error. Please try again.", "error");
		setRowDisabled(row, false);
		return;
	}
	await loadPoolButtons();
}

async function init() {
	renderVms();
	if (vms.length) {
		await Promise.all(vms.map(validateVm));
		renderVms();
	}
	resumeTickets();

	if (claimLabel) {
		renderButtons([{ text: `Claim ${claimLabel}`, name: claimLabel }]);
		if (vms.length === 0) handleTerminalAccess(claimLabel);
		return;
	}

	await loadPoolButtons();
}

function resumeTickets() {
	const tickets = loadTickets();
	if (!tickets.length) return;
	claimProgress.hidden = false;
	setStatus("Checking on your machine...");
	for (const pending of tickets) pollTicket(pending.ticket, pending.pool, mainSink);
}

async function handleTerminalAccess(poolName, btn) {
	if (atCap()) {
		openSwapModal(() => handleTerminalAccess(poolName, btn));
		return;
	}
	setButtonsDisabled(true);
	setStatus("Assigning your machine...");

	try {
		const claimBody = poolName ? { pool: poolName } : {};
		if (claimCode) claimBody.code = claimCode;
		const response = await fetch("/api/claim", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify(claimBody)
		});
		const data = await response.json();

		if (data.status === "provisioning") {
			claimProgress.hidden = false;
			setStatus(stageText("cloning"));
			pollTicket(data.ticket, poolName, mainSink);
		} else if (response.ok) {
			addVm(data.url, poolName, data.expires_at);
			setStatus("VM claimed! Redirecting...", "success");
			window.location.href = data.url;
		} else {
			if (data.code_required) openCodeDialog();
			else setStatus(data.detail || "Claim failed. Please try again.", "error");
			setButtonsDisabled(false);
		}
	} catch (error) {
		setStatus("Network error. Please try again.", "error");
		setButtonsDisabled(false);
	}
}

function stageText(stage) {
	if (stage === "cloning") return "Cloning template...";
	if (stage === "booting") return "Booting VM...";
	if (stage === "network") return "Waiting for network...";
	if (stage === "adding") return "Adding machine to pool...";
	return "Preparing your machine...";
}

const mainSink = {
	stage: (stage) => setStatus(stageText(stage)),
	ready: (data, pool) => {
		addVm(data.url, data.pool || pool, data.expires_at);
		setStatus("VM ready! Redirecting...", "success");
		window.location.href = data.url;
	},
	fail: (detail) => {
		claimProgress.hidden = true;
		setStatus(detail, "error");
		setButtonsDisabled(false);
	},
};

const dialogSink = {
	stage: (stage) => { codeStage.textContent = stageText(stage); },
	ready: (data, pool) => {
		addVm(data.url, data.pool || pool, data.expires_at);
		codeStage.textContent = "Redirecting...";
		window.location.href = data.url;
	},
	fail: (detail) => redeemFailed(detail),
};

function pollTicket(ticket, pool, sink) {
	const pending = addPending(ticket, pool);
	polls.set(ticket, sink);
	let poll = null;
	const finish = () => {
		clearInterval(poll);
		polls.delete(ticket);
		dropPending(ticket);
		if (dialogTicket === ticket) dialogTicket = null;
	};
	poll = setInterval(async () => {
		const current = polls.get(ticket);
		if (!current) {
			clearInterval(poll);
			return;
		}
		let data = null;
		let failed = null;
		if (Date.now() - pending.started > PROVISION_TIMEOUT_MS) {
			failed = "Provisioning is taking too long. Please contact your instructor.";
		} else {
			try {
				const response = await fetch(`/api/redeem/${ticket}`);
				data = await response.json();
				if (!response.ok || data.status === "error") {
					failed = data.detail || "Provisioning failed. Please try again.";
				}
			} catch (error) {}
		}
		if (failed) {
			finish();
			current.fail(failed);
			return;
		}
		if (data && data.stage) current.stage(data.stage);
		if (data && data.status === "ready") {
			finish();
			current.ready(data, pool);
		}
	}, 3000);
}

function redeemFailed(detail) {
	codeProgress.hidden = true;
	codeForm.hidden = false;
	codeInput.disabled = false;
	codeSubmitBtn.disabled = false;
	codeError.textContent = detail || "";
	codeInput.focus();
}

async function redeemCode() {
	if (atCap()) {
		const code = codeInput.value.trim();
		codeDialog.close();
		openSwapModal(() => {
			openCodeDialog();
			codeInput.value = code;
			redeemCode();
		});
		return;
	}
	if (dialogTicket && polls.has(dialogTicket)) {
		redeemFailed("A machine is already being prepared. Please wait or press Cancel.");
		return;
	}
	const code = codeInput.value.trim();
	if (!code) return;
	codeError.textContent = "";
	codeInput.disabled = true;
	codeSubmitBtn.disabled = true;
	codeForm.hidden = true;
	codeProgress.hidden = false;
	codeStage.textContent = "Checking code...";
	try {
		const response = await fetch("/api/redeem", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ code })
		});
		const data = await response.json();
		if (response.ok && data.status === "ready") {
			addVm(data.url, data.pool, data.expires_at);
			codeStage.textContent = "Redirecting...";
			window.location.href = data.url;
		} else if (response.ok && data.status === "provisioning") {
			dialogTicket = data.ticket;
			codeStage.textContent = stageText("cloning");
			pollTicket(data.ticket, data.pool, dialogSink);
		} else {
			redeemFailed(data.detail || "Unknown code.");
		}
	} catch (error) {
		redeemFailed("Network error. Please try again.");
	}
}

function cancelRedeem() {
	codeProgress.hidden = true;
	codeForm.hidden = false;
	codeInput.disabled = false;
	codeSubmitBtn.disabled = false;
	codeInput.value = "";
	codeError.textContent = "";
	if (dialogTicket && polls.has(dialogTicket)) {
		polls.set(dialogTicket, mainSink);
		claimProgress.hidden = false;
		setStatus("Your machine is still being prepared. It will appear here when ready.");
		dialogTicket = null;
	}
}

function openCodeDialog() {
	codeForm.hidden = false;
	codeProgress.hidden = true;
	codeError.textContent = "";
	codeInput.value = "";
	codeInput.disabled = false;
	codeSubmitBtn.disabled = false;
	codeDialog.showModal();
	codeInput.focus();
}

function openSwapModal(claimFn) {
	pendingClaim = claimFn;
	swapError.textContent = "";
	swapOptions.innerHTML = "";
	for (const vm of vms) {
		const row = document.createElement("div");
		row.className = "swap-row";
		const label = document.createElement("span");
		label.className = "vm-label";
		label.innerText = vm.pool || "Workshop VM";
		const btn = document.createElement("button");
		btn.className = "danger";
		btn.innerText = "Release & claim";
		btn.onclick = () => swapRelease(vm, btn);
		row.append(label, btn);
		swapOptions.appendChild(row);
	}
	swapDialog.showModal();
}

async function swapRelease(vm, btn) {
	btn.disabled = true;
	try {
		const response = await fetch("/api/release", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ url: vm.url })
		});
		if (!response.ok && response.status !== 404) {
			const data = await response.json();
			swapError.textContent = data.detail || "Release failed. Please try again.";
			btn.disabled = false;
			return;
		}
		removeVm(vm);
		swapDialog.close();
		const claim = pendingClaim;
		pendingClaim = null;
		if (claim) claim();
	} catch (error) {
		swapError.textContent = "Network error. Please try again.";
		btn.disabled = false;
	}
}

codeBtn.addEventListener("click", openCodeDialog);
codeForm.addEventListener("submit", (e) => {
	e.preventDefault();
	redeemCode();
});
codeCancelBtn.addEventListener("click", () => codeDialog.close());
codeDialog.addEventListener("close", cancelRedeem);
swapCancelBtn.addEventListener("click", () => swapDialog.close());
swapDialog.addEventListener("close", () => {
	pendingClaim = null;
	for (const btn of swapOptions.querySelectorAll("button")) btn.disabled = false;
});

init();

function selectRandomBackground() {
	let backgrounds = ["bliss.jpg", "macos.jpg", "penguins.png", "trig.png"];
	let index = Math.floor(Math.random() * backgrounds.length);
	document.body.style.backgroundImage = `url('/images/${backgrounds[index]}')`;
}
selectRandomBackground();
