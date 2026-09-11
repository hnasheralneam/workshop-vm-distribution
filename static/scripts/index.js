const MAX_VMS = 2;
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
let redeemPoll = null;
let redeemGen = 0;
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

function addVm(url, pool) {
	vms.push({ url, pool: pool || null });
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

function setStatus(text) {
	status.style.display = "block";
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
	}
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
	if (atCap()) {
		buttonsContainer.innerHTML = "";
		codeBtn.hidden = true;
		setStatus(`You already have ${MAX_VMS} machines. Release one to claim another.`);
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
		if (data.valid) return;
		if (data.url) {
			vm.url = data.url;
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
			saveVms();
			setStatus("Redirecting...");
			window.location.href = data.url;
		} else if (data.expired) {
			removeVm(vm);
			renderVms();
			setStatus("Your machine expired. Claim a new one below.");
			if (!atCap()) await loadPoolButtons();
		} else {
			setStatus(data.detail || "Reconnect failed. Please try again.");
			setRowDisabled(row, false);
		}
	} catch (error) {
		setStatus("Network error. Please try again.");
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
			setStatus("Machine released.");
		} else {
			const data = await response.json();
			setStatus(data.detail || "Release failed. Please try again.");
			setRowDisabled(row, false);
			return;
		}
	} catch (error) {
		setStatus("Network error. Please try again.");
		setRowDisabled(row, false);
		return;
	}
	if (!atCap()) await loadPoolButtons();
}

async function init() {
	renderVms();
	if (vms.length) {
		buttonsContainer.innerHTML = "";
		codeBtn.hidden = true;
		await Promise.all(vms.map(validateVm));
		renderVms();
	}
	if (atCap()) return;

	if (claimLabel) {
		renderButtons([{ text: `Claim ${claimLabel}`, name: claimLabel }]);
		handleTerminalAccess(claimLabel);
		return;
	}

	await loadPoolButtons();
}

async function handleTerminalAccess(poolName, btn) {
	if (atCap()) {
		setStatus(`You already have ${MAX_VMS} machines. Release one to claim another.`);
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
			pollClaim(data.ticket, poolName);
		} else if (response.ok) {
			addVm(data.url, poolName);
			setStatus("VM claimed! Redirecting...");

			window.location.href = data.url;
		} else {
			if (data.code_required) openCodeDialog();
			else setStatus(data.detail);
			setButtonsDisabled(false);
		}
	} catch (error) {
		setStatus("Network error. Please try again.");
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

function pollClaim(ticket, poolName) {
	setStatus(stageText("cloning"));
	const poll = setInterval(async () => {
		try {
			const response = await fetch(`/api/redeem/${ticket}`);
			const data = await response.json();
			if (!response.ok || data.status === "error") {
				clearInterval(poll);
				setStatus(data.detail || "Provisioning failed. Please try again.");
				setButtonsDisabled(false);
				return;
			}
			if (data.stage) setStatus(stageText(data.stage));
			if (data.status === "ready") {
				clearInterval(poll);
				addVm(data.url, data.pool || poolName);
				setStatus("VM ready! Redirecting...");
				window.location.href = data.url;
			}
		} catch (error) {}
	}, 3000);
}

function finishRedeem(url, pool) {
	if (redeemPoll) clearInterval(redeemPoll);
	addVm(url, pool);
	codeStage.textContent = "Redirecting...";
	window.location.href = url;
}

function redeemFailed(detail) {
	if (redeemPoll) clearInterval(redeemPoll);
	codeProgress.hidden = true;
	codeForm.hidden = false;
	codeInput.disabled = false;
	codeSubmitBtn.disabled = false;
	codeError.textContent = detail;
	codeInput.focus();
}

function pollRedeem(ticket, gen, pool) {
	codeForm.hidden = true;
	codeProgress.hidden = false;
	redeemPoll = setInterval(async () => {
		if (gen !== redeemGen) {
			clearInterval(redeemPoll);
			return;
		}
		try {
			const response = await fetch(`/api/redeem/${ticket}`);
			const data = await response.json();
			if (!response.ok) {
				redeemFailed(data.detail || "Lost track of your machine. Please try again.");
				return;
			}
			if (data.stage) codeStage.textContent = stageText(data.stage);
			if (data.status === "ready") finishRedeem(data.url, data.pool || pool);
			else if (data.status === "error") redeemFailed(data.detail || "Provisioning failed. Please try again.");
		} catch (error) {}
	}, 3000);
}

async function redeemCode() {
	if (atCap()) {
		redeemFailed(`You already have ${MAX_VMS} machines. Release one first.`);
		return;
	}
	const code = codeInput.value.trim();
	if (!code) return;
	const gen = ++redeemGen;
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
		if (gen !== redeemGen) return;
		const data = await response.json();
		if (response.ok && data.status === "ready") {
			finishRedeem(data.url, data.pool);
		} else if (response.ok && data.status === "provisioning") {
			pollRedeem(data.ticket, gen, data.pool);
		} else {
			redeemFailed(data.detail || "Unknown code.");
		}
	} catch (error) {
		if (gen === redeemGen) redeemFailed("Network error. Please try again.");
	}
}

function cancelRedeem() {
	redeemGen += 1;
	if (redeemPoll) {
		clearInterval(redeemPoll);
		redeemPoll = null;
	}
	codeProgress.hidden = true;
	codeForm.hidden = false;
	codeInput.disabled = false;
	codeSubmitBtn.disabled = false;
	codeInput.value = "";
	codeError.textContent = "";
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
codeBtn.addEventListener("click", openCodeDialog);
codeForm.addEventListener("submit", (e) => {
	e.preventDefault();
	redeemCode();
});
codeCancelBtn.addEventListener("click", () => codeDialog.close());
codeDialog.addEventListener("close", cancelRedeem);

init();

function selectRandomBackground() {
	let backgrounds = ["bliss.jpg", "macos.jpg", "penguins.png", "trig.png"];
	let index = Math.round(Math.random() * (backgrounds.length - 1));
	document.body.style.backgroundImage = `url('/images/${backgrounds[index]}')`;
}
selectRandomBackground();
