"""Run a command inside a guest through qemu-guest-agent.

The portal only needs this for one thing: the huitzilopochtli prebind handoff,
where ``huitz join`` runs inside the ranked VM when the player opens its link.
The technique mirrors ``pmx.py``: one ``agent exec`` then poll ``exec-status``.
Never retry a guest-agent call that already answered -- repeated failed spawns
wedge qga. Waiting for the agent uses a read-only ping, which spawns nothing
and is safe to poll.
"""
import time

import requests


class GuestError(Exception):
    pass


def _retry_transport(fn, attempts=3, delay_s=3.0):
    last = None
    for _ in range(attempts):
        try:
            return fn()
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError) as exc:
            last = exc
            time.sleep(delay_s)
    raise last


def wait_ready(proxmox, node_name, vmid, timeout_s=120, poll_s=4):
    """Block until the guest agent answers a read-only ping.

    A just-cloned VM boots for a while before qga listens, and an exec against
    the not-yet-listening socket surfaces as 5xx/596 noise (broken pipe). A
    ping spawns nothing in the guest, so polling it cannot wedge the agent.
    Raises GuestError when the agent never comes up."""
    deadline = time.time() + timeout_s
    while True:
        try:
            proxmox.nodes(node_name).qemu(vmid).agent("ping").post()
            return
        except Exception as exc:
            if time.time() >= deadline:
                raise GuestError(
                    f"guest agent not ready after {timeout_s}s: {exc}") from exc
            time.sleep(poll_s)


def run(proxmox, node_name, vmid, argv, timeout_s=90, poll_s=2):
    """Execute ``argv`` as root in the guest and return the exec-status dict.

    Waits for the agent (read-only ping) first, so a booting clone cannot
    dead-end on a broken-pipe exec. Raises GuestError when the agent cannot
    start the command or the command does not exit before ``timeout_s`` (its
    own exit code is in the returned ``exitcode``, so 0/1 is the caller's
    business)."""
    wait_ready(proxmox, node_name, vmid)
    result = None
    # One guarded respawn: an exec that raised never spawned anything, and the
    # re-check ping confirms the agent is still healthy before trying again.
    for attempt in (1, 2):
        try:
            result = _retry_transport(
                lambda: proxmox.nodes(node_name).qemu(vmid).agent("exec").post(
                    command=list(argv)))
            break
        except Exception as exc:
            if attempt == 2:
                raise GuestError(f"guest agent exec failed: {exc}") from exc
            try:
                wait_ready(proxmox, node_name, vmid, timeout_s=30)
            except GuestError:
                raise GuestError(f"guest agent exec failed: {exc}") from None
    pid = result.get("pid") if isinstance(result, dict) else None
    if not pid:
        raise GuestError("guest agent returned no pid")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            status = _retry_transport(
                lambda: proxmox.nodes(node_name).qemu(vmid).agent(
                    "exec-status").get(pid=pid))
        except Exception as exc:
            raise GuestError(f"guest agent status failed: {exc}") from exc
        if status.get("exited"):
            return status
        time.sleep(poll_s)
    raise GuestError(f"command did not finish within {timeout_s}s")
