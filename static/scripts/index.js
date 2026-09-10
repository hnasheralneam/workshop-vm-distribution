let existingUrl = localStorage.getItem("assigned_vm_url");
const buttonsContainer = document.getElementById("claim-buttons");
const status = document.getElementById("status-message");
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
		const pools = (data.pools || []).filter((p) => p.dispenser || p.available > 0);
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

async function init() {
	if (existingUrl) {
		renderButtons([{ text: "Reconnect", name: null }]);
		setStatus("You already have an assigned machine.");
		setButtonsDisabled(true);
		await checkExistingVm();
	}
	if (existingUrl) {
		if (claimLabel) await handlePoolSwitch(claimLabel);
		return;
	}

	if (claimLabel) {
		renderButtons([{ text: `Claim ${claimLabel}`, name: claimLabel }]);
		handleTerminalAccess(claimLabel);
		return;
	}

	await loadPoolButtons();
}

async function checkExistingVm() {
	try {
		const response = await fetch("/api/validate", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ url: existingUrl })
		});
		const data = await response.json();

			if (!data.valid) {
				if (data.url) {
					localStorage.setItem("assigned_vm_url", data.url);
					existingUrl = data.url;
					renderButtons([{ text: "Reconnect", name: null }]);
					setStatus("Your previous machine expired. A new machine has been assigned.");
				} else {
					localStorage.removeItem("assigned_vm_url");
					existingUrl = null;
					setStatus(data.expired
						? "Your previous machine expired and no new machines are available. Please contact your instructor."
						: "Your previous machine is no longer available. Claim a new one below.");
					await loadPoolButtons();
				}
			}
	} catch (error) {
		setStatus("Network error checking your machine. Please try again.");
	} finally {
		setButtonsDisabled(false);
	}
}

async function handleTerminalAccess(poolName, btn) {
	if (existingUrl) {
		setButtonsDisabled(true);
		setStatus("Reconnecting...");
		try {
			const response = await fetch("/api/reconnect", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ url: existingUrl })
			});
			const data = await response.json();
			if (response.ok && data.url) {
				localStorage.setItem("assigned_vm_url", data.url);
				existingUrl = data.url;
				setStatus("Redirecting...");
				window.location.href = data.url;
			} else if (data.expired) {
				localStorage.removeItem("assigned_vm_url");
				existingUrl = null;
				setStatus("Your machine expired. Claim a new one below.");
				await init();
			} else {
				setStatus(data.detail || "Reconnect failed. Please try again.");
				setButtonsDisabled(false);
			}
		} catch (error) {
			setStatus("Network error. Please try again.");
			setButtonsDisabled(false);
		}
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
			pollClaim(data.ticket);
		} else if (response.ok) {
			localStorage.setItem("assigned_vm_url", data.url);
			setStatus("VM claimed! Redirecting...");

			window.location.href = data.url;
		} else {
			setStatus(data.detail);
			setButtonsDisabled(false);
		}
	} catch (error) {
		setStatus("Network error. Please try again.");
		setButtonsDisabled(false);
	}
}

async function handlePoolSwitch(label) {
	if (!confirm(`Claiming a machine from pool "${label}" will immediately and permanently delete your current machine. Continue?`)) {
		setButtonsDisabled(false);
		return;
	}
	setButtonsDisabled(true);
	setStatus("Assigning your machine...");

	try {
		const claimBody = { pool: label };
		if (claimCode) claimBody.code = claimCode;
		const response = await fetch("/api/claim", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify(claimBody)
		});
		const data = await response.json();
		if (!response.ok) {
			setStatus(data.detail);
			setButtonsDisabled(false);
			return;
		}

		localStorage.setItem("assigned_vm_url", data.url);
		const oldUrl = existingUrl;
		existingUrl = data.url;
		try {
			await fetch("/api/release", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ url: oldUrl })
			});
		} catch (error) {}

		setStatus("VM claimed! Redirecting...");
		window.location.href = data.url;
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

function pollClaim(ticket) {
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
				localStorage.setItem("assigned_vm_url", data.url);
				existingUrl = data.url;
				setStatus("VM ready! Redirecting...");
				window.location.href = data.url;
			}
		} catch (error) {}
	}, 3000);
}

function finishRedeem(url) {
	if (redeemPoll) clearInterval(redeemPoll);
	const oldUrl = localStorage.getItem("assigned_vm_url");
	localStorage.setItem("assigned_vm_url", url);
	existingUrl = url;
	if (oldUrl) {
		fetch("/api/release", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ url: oldUrl })
		}).catch(() => {});
	}
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

function pollRedeem(ticket, gen) {
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
			if (data.status === "ready") finishRedeem(data.url);
			else if (data.status === "error") redeemFailed(data.detail || "Provisioning failed. Please try again.");
		} catch (error) {}
	}, 3000);
}

async function redeemCode() {
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
			finishRedeem(data.url);
		} else if (response.ok && data.status === "provisioning") {
			pollRedeem(data.ticket, gen);
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

codeBtn.addEventListener("click", () => {
	codeForm.hidden = false;
	codeProgress.hidden = true;
	codeError.textContent = "";
	codeInput.value = "";
	codeInput.disabled = false;
	codeSubmitBtn.disabled = false;
	codeDialog.showModal();
	codeInput.focus();
});
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
