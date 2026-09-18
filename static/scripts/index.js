let MAX_VMS = 2;
const PROVISION_TIMEOUT_MS = 600000;
const buttonsContainer = document.getElementById("claim-buttons");
const status = document.getElementById("status-message");
const subtitle = document.getElementById("portal-subtitle");
const vmsHeading = document.getElementById("vms-heading");
const vmsContainer = document.getElementById("vms");
const codeBtn = document.getElementById("code-btn");
const codeDialog = document.getElementById("code-dialog");
const codeForm = document.getElementById("code-form");
const codeInput = document.getElementById("code-input");
const codeError = document.getElementById("code-error");
const codeSubmitBtn = document.getElementById("code-submit-btn");
const codeCancelBtn = document.getElementById("code-cancel-btn");
const swapDialog = document.getElementById("swap-dialog");
const swapSubtitle = document.getElementById("swap-subtitle");
const swapOptions = document.getElementById("swap-options");
const swapError = document.getElementById("swap-error");
const swapCancelBtn = document.getElementById("swap-cancel-btn");
const provisionDialog = document.getElementById("provision-dialog");
const provisionOrbit = document.getElementById("provision-orbit");
const provisionLabel = document.getElementById("provision-label");
const provisionLines = document.getElementById("provision-lines");
const provisionError = document.getElementById("provision-error");
const provisionCloseBtn = document.getElementById("provision-close-btn");
const polls = new Map();
const modalTickets = new Map();
let pendingClaim = null;
let provisionErrorMode = false;
let lastProvisionError = "";
let celebrating = false;
let vms = loadVms();

function loadVms() {
	try {
		const parsed = JSON.parse(localStorage.getItem("assigned_vm_urls"));
		if (Array.isArray(parsed)) return parsed;
	} catch (error) {}
	return [];
}

function saveVms() {
	localStorage.setItem("assigned_vm_urls", JSON.stringify(vms));
}

function addVm(url, pool, expiresAt, uid) {
	vms.push({ url, pool: pool || null, uid: uid || null, expires_at: expiresAt ?? null });
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
const onClaimPage = /^\/claim(\/|$)/.test(location.pathname);

function renderButtons(pools) {
	buttonsContainer.innerHTML = "";
	for (const pool of pools) {
		const btn = document.createElement("button");
		btn.className = "claim-btn";
		btn.innerText = pool.text;
		btn.onclick = () => handleTerminalAccess(pool, btn);
		buttonsContainer.appendChild(btn);
	}
}

function setButtonsDisabled(disabled) {
	for (const btn of buttonsContainer.querySelectorAll("button")) {
		btn.disabled = disabled;
	}
}

function applyTypes(data) {
	MAX_VMS = data.max_vms || 2;
	subtitle.innerText = `Claim up to ${MAX_VMS} personal workshop VMs below.`;
}

async function loadPoolButtons(attempt = 0) {
	try {
		const response = await fetch("/api/types");
		const data = await response.json();
		applyTypes(data);
		codeBtn.hidden = !(data.coded > 0);
		const pools = (data.pools || []).filter((p) => !p.private && p.code && (p.dispenser || p.available > 0));
		if (pools.length > 1) {
			renderButtons(pools.map((p) => ({ text: `Claim ${p.name}`, name: p.name, code: p.code })));
		} else if (pools.length === 1) {
			renderButtons([{ text: "Claim", name: pools[0].name, code: pools[0].code }]);
		} else {
			buttonsContainer.innerHTML = "";
			setStatus("No VMs are available right now. Please contact your instructor.");
		}
	} catch (error) {
		renderButtons([{ text: "Claim", name: null, code: claimCode }]);
		codeBtn.hidden = false;
		if (attempt < 2) {
			setTimeout(() => loadPoolButtons(attempt + 1), 5000);
		} else {
			setStatus("Could not load the pool list. You can still try claiming below.", "error");
		}
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
			body: JSON.stringify({ url: vm.url, uid: vm.uid })
		});
		const data = await response.json();
		if (data.valid) {
			if (data.uid) vm.uid = data.uid;
			if (data.url) vm.url = data.url;
			if (data.expires_at) vm.expires_at = data.expires_at;
			if (data.uid || data.url || data.expires_at) saveVms();
			return;
		}
		if (data.url) {
			vm.url = data.url;
			vm.uid = data.uid || null;
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
			body: JSON.stringify({ url: vm.url, uid: vm.uid })
		});
		const data = await response.json();
		if (response.ok && data.url) {
			vm.url = data.url;
			if (data.uid) vm.uid = data.uid;
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
			body: JSON.stringify({ url: vm.url, uid: vm.uid })
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

	if (onClaimPage && claimCode) {
		renderButtons([{ text: claimLabel ? `Claim ${claimLabel}` : "Claim", name: claimLabel, code: claimCode }]);
		if (vms.length) {
			try {
				const response = await fetch("/api/types");
				applyTypes(await response.json());
			} catch (error) {}
		}
		if (!loadTickets().length) claimByCode(claimCode);
		return;
	}

	await loadPoolButtons();
}

function resumeTickets() {
	const tickets = loadTickets();
	if (!tickets.length) return;
	for (const pending of tickets) {
		provisionStart(pending.ticket, pending.pool);
		pollTicket(pending.ticket, pending.pool, modalSink(pending.ticket));
	}
}

async function handleTerminalAccess(pool, btn) {
	if (atCap()) {
		openSwapModal(() => handleTerminalAccess(pool, btn));
		return;
	}
	setButtonsDisabled(true);
	setStatus("Assigning your machine...");

	try {
		const claimBody = {};
		const code = pool && pool.code ? pool.code : claimCode;
		if (code) claimBody.code = code;
		const response = await fetch("/api/claim", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify(claimBody)
		});
		const data = await response.json();

		if (data.status === "provisioning") {
			provisionStart(data.ticket, data.pool);
			pollTicket(data.ticket, data.pool, modalSink(data.ticket));
		} else if (response.ok) {
			addVm(data.url, data.pool || (pool && pool.name), data.expires_at, data.uid);
			setStatus("VM claimed! Redirecting...", "success");
			window.location.replace(data.url);
		} else {
			setStatus(data.detail || "Claim failed. Please try again.", "error");
			setButtonsDisabled(false);
		}
	} catch (error) {
		setStatus("Network error. Please try again.", "error");
		setButtonsDisabled(false);
	}
}

async function claimByCode(code) {
	if (atCap()) {
		openSwapModal(() => claimByCode(code));
		return;
	}
	if (polls.size) return;
	setButtonsDisabled(true);
	setStatus("Assigning your machine...");

	try {
		const response = await fetch("/api/redeem", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ code })
		});
		const data = await response.json();

		if (response.ok && data.status === "ready") {
			addVm(data.url, data.pool, data.expires_at, data.uid);
			setStatus("VM claimed! Redirecting...", "success");
			window.location.replace(data.url);
		} else if (response.ok && data.status === "provisioning") {
			provisionStart(data.ticket, data.pool);
			pollTicket(data.ticket, data.pool, modalSink(data.ticket));
		} else {
			setStatus(data.detail || "Claim failed. Please try again.", "error");
			await loadPoolButtons();
		}
	} catch (error) {
		setStatus("Network error. Please try again.", "error");
		await loadPoolButtons();
	}
}

function stageText(stage) {
	if (stage === "cloning") return "Cloning template...";
	if (stage === "booting") return "Booting VM...";
	if (stage === "network") return "Waiting for network...";
	if (stage === "adding") return "Adding machine to pool...";
	return "Preparing your machine...";
}

const STAGES = ["cloning", "booting", "network", "adding"];
const orbitArcs = provisionOrbit.querySelectorAll(".arc");
const orbitIcons = provisionOrbit.querySelectorAll(".ico");

function orbitSet(stage) {
	provisionOrbit.classList.remove("s-done", "s-error");
	for (const arc of orbitArcs) arc.classList.remove("lit", "bad");
	for (const icon of orbitIcons) icon.classList.remove("done", "now");
	if (stage === "done") {
		for (const arc of orbitArcs) arc.classList.add("lit");
		for (const icon of orbitIcons) icon.classList.add("done");
		provisionOrbit.classList.add("s-done");
		return;
	}
	if (stage === "error") {
		for (const arc of orbitArcs) arc.classList.add("bad");
		provisionOrbit.classList.add("s-error");
		return;
	}
	const upto = STAGES.indexOf(stage);
	for (let i = 0; i < upto; i++) {
		orbitArcs[i].classList.add("lit");
		orbitIcons[i].classList.add("done");
	}
	if (upto >= 0) orbitIcons[upto].classList.add("now");
}

function setStageLabel(el, text) {
	el.textContent = "";
	const base = document.createElement("span");
	base.innerText = text.replace(/\.\.\.$/, "");
	const dots = document.createElement("span");
	dots.className = "dots";
	for (let i = 0; i < 3; i++) {
		const dot = document.createElement("i");
		dot.innerText = ".";
		dots.appendChild(dot);
	}
	el.append(base, dots);
}

function syncProvisionOrbit() {
	let min = Infinity;
	for (const info of modalTickets.values()) min = Math.min(min, info.rank);
	const stage = modalTickets.size ? STAGES[min] : null;
	orbitSet(stage);
	if (modalTickets.size) setStageLabel(provisionLabel, stageText(stage));
	else provisionLabel.textContent = "";
}

function renderProvisionLines() {
	provisionLines.innerHTML = "";
	if (modalTickets.size < 2) return;
	for (const [ticket, info] of modalTickets) {
		const row = document.createElement("div");
		row.className = "pline";
		const dot = document.createElement("span");
		dot.className = "pdot";
		const text = document.createElement("span");
		setStageLabel(text, (info.pool ? `${info.pool}: ` : "") + stageText(STAGES[info.rank]));
		row.append(dot, text);
		provisionLines.appendChild(row);
	}
}

function provisionStart(ticket, pool) {
	if (!provisionDialog.open) {
		celebrating = false;
		provisionErrorMode = false;
		lastProvisionError = "";
		provisionError.textContent = "";
		provisionCloseBtn.hidden = true;
		provisionDialog.showModal();
	}
	modalTickets.set(ticket, { rank: 0, pool: pool || null });
	syncProvisionOrbit();
	renderProvisionLines();
}

function provisionStage(ticket, stage) {
	if (celebrating) return;
	const info = modalTickets.get(ticket);
	if (!info) return;
	info.rank = Math.max(info.rank, STAGES.indexOf(stage));
	syncProvisionOrbit();
	renderProvisionLines();
}

function provisionReady(ticket, data, pool) {
	modalTickets.delete(ticket);
	addVm(data.url, data.pool || pool, data.expires_at, data.uid);
	if (!provisionDialog.open || celebrating) {
		window.location.replace(data.url);
		return;
	}
	celebrating = true;
	provisionLines.innerHTML = "";
	orbitSet("done");
	setStageLabel(provisionLabel, "VM ready! Opening...");
	setTimeout(() => { window.location.replace(data.url); }, 1200);
}

function provisionFail(ticket, detail) {
	if (celebrating) return;
	modalTickets.delete(ticket);
	renderProvisionLines();
	if (modalTickets.size) {
		provisionError.textContent = detail;
		return;
	}
	if (provisionDialog.open) {
		orbitSet("error");
		provisionLabel.innerText = "";
		provisionError.textContent = detail;
		provisionCloseBtn.hidden = false;
		provisionErrorMode = true;
		lastProvisionError = detail;
	} else {
		setStatus(detail, "error");
		setButtonsDisabled(false);
	}
}

function modalSink(ticket) {
	return {
		stage: (stage) => provisionStage(ticket, stage),
		ready: (data, pool) => provisionReady(ticket, data, pool),
		fail: (detail) => provisionFail(ticket, detail),
	};
}

const backgroundSink = {
	stage: (stage) => setStatus(stageText(stage)),
	ready: (data, pool) => {
		addVm(data.url, data.pool || pool, data.expires_at, data.uid);
		setStatus("VM ready! Redirecting...", "success");
		window.location.replace(data.url);
	},
	fail: (detail) => {
		setStatus(detail, "error");
		setButtonsDisabled(false);
	},
};

function pollTicket(ticket, pool, sink) {
	const pending = addPending(ticket, pool);
	polls.set(ticket, sink);
	let poll = null;
	const finish = () => {
		clearInterval(poll);
		polls.delete(ticket);
		dropPending(ticket);
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
	if (polls.size) {
		redeemFailed("A machine is already being prepared. Please wait.");
		return;
	}
	const code = codeInput.value.trim();
	if (!code) return;
	codeError.textContent = "";
	codeInput.disabled = true;
	codeSubmitBtn.disabled = true;
	try {
		const response = await fetch("/api/redeem", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ code })
		});
		const data = await response.json();
		if (response.ok && data.status === "ready") {
			addVm(data.url, data.pool, data.expires_at, data.uid);
			window.location.href = data.url;
		} else if (response.ok && data.status === "provisioning") {
			codeDialog.close();
			provisionStart(data.ticket, data.pool);
			pollTicket(data.ticket, data.pool, modalSink(data.ticket));
		} else {
			redeemFailed(data.detail || "Unknown code.");
		}
	} catch (error) {
		redeemFailed("Network error. Please try again.");
	}
}

function openCodeDialog() {
	codeForm.hidden = false;
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
	swapSubtitle.innerText = `You already have ${MAX_VMS} machines. Release one to claim another.`;
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
			body: JSON.stringify({ url: vm.url, uid: vm.uid })
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
swapCancelBtn.addEventListener("click", () => swapDialog.close());
swapDialog.addEventListener("close", () => {
	pendingClaim = null;
	for (const btn of swapOptions.querySelectorAll("button")) btn.disabled = false;
});
provisionCloseBtn.addEventListener("click", () => {
	releaseProvision();
	provisionDialog.close();
});
provisionDialog.addEventListener("close", releaseProvision);
provisionDialog.addEventListener("cancel", (e) => {
	if (modalTickets.size && !celebrating) e.preventDefault();
});

function releaseProvision() {
	if (celebrating) return;
	for (const ticket of modalTickets.keys()) {
		if (polls.has(ticket)) polls.set(ticket, backgroundSink);
	}
	modalTickets.clear();
	if (provisionErrorMode) {
		setStatus(lastProvisionError, "error");
		setButtonsDisabled(false);
	}
	orbitSet(null);
	provisionError.textContent = "";
	provisionLabel.innerText = "";
	provisionLines.innerHTML = "";
	provisionErrorMode = false;
}

init();

function selectRandomBackground() {
	let backgrounds = ["bliss.jpg", "macos.jpg", "penguins.png", "trig.png"];
	let index = Math.floor(Math.random() * backgrounds.length);
	document.body.style.backgroundImage = `url('/images/${backgrounds[index]}')`;
}
selectRandomBackground();
