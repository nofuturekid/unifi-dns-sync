#!/usr/bin/env python3
"""
unifi-dns-sync: Listens for Docker container start/stop/die events and
automatically creates/deletes CNAME records in UniFi's local DNS.

Each container with its own IP (macvlan) gets a CNAME pointing to
the NPM host (e.g. plex.kroll-home.de -> npm.kroll-home.de).

Containers without a dedicated IP (bridge network on host IP) are skipped.
"""

import os
import signal
import sys
import time
import logging
import docker
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_shutdown = False

def _handle_signal(signum, frame):
    global _shutdown
    log.info("Received signal %s, shutting down...", signum)
    _shutdown = True

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

# ---------------------------------------------------------------------------
# Configuration (override via environment variables)
# ---------------------------------------------------------------------------
UNIFI_HOST      = os.getenv("UNIFI_HOST", "https://192.168.11.1")
UNIFI_API_KEY   = os.getenv("UNIFI_API_KEY", "")
UNIFI_SITE      = os.getenv("UNIFI_SITE", "default")
DOMAIN          = os.getenv("DOMAIN", "kroll-home.de")
NPM_CNAME_TARGET = os.getenv("NPM_CNAME_TARGET", f"npm.{os.getenv('DOMAIN', 'kroll-home.de')}")
# Comma-separated list of network names considered "host" (skip these)
SKIP_NETWORKS   = set(os.getenv("SKIP_NETWORKS", "bridge,host,none").split(","))
# Comma-separated list of container names to always skip
SKIP_CONTAINERS = set(os.getenv("SKIP_CONTAINERS", "").split(","))
LOG_LEVEL       = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# UniFi API helpers
# ---------------------------------------------------------------------------
BASE_URL = f"{UNIFI_HOST}/proxy/network/v2/api/site/{UNIFI_SITE}/static-dns"
HEADERS = {
    "X-API-KEY": UNIFI_API_KEY,
    "Accept": "application/json",
    "Content-Type": "application/json",
}


def _get_all_records() -> list[dict]:
    """Return all static DNS records from UniFi."""
    r = requests.get(BASE_URL, headers=HEADERS, verify=False, timeout=10)
    r.raise_for_status()
    return r.json()


def _find_record(fqdn: str) -> dict | None:
    """Find an existing CNAME record by FQDN. Returns the record dict or None."""
    for record in _get_all_records():
        if record.get("key") == fqdn and record.get("record_type") == "CNAME":
            return record
    return None


def create_cname(container_name: str) -> bool:
    """Create a CNAME record for container_name.domain -> NPM_CNAME_TARGET."""
    fqdn = f"{container_name.lower()}.{DOMAIN}"
    existing = _find_record(fqdn)
    if existing:
        log.info("CNAME %s already exists, skipping.", fqdn)
        return True

    payload = {
        "key": fqdn,
        "record_type": "CNAME",
        "value": NPM_CNAME_TARGET,
        "enabled": True,
    }
    r = requests.post(BASE_URL, headers=HEADERS, json=payload, verify=False, timeout=10)
    if r.ok:
        log.info("Created CNAME: %s -> %s", fqdn, NPM_CNAME_TARGET)
        return True
    else:
        log.error("Failed to create CNAME %s: %s %s", fqdn, r.status_code, r.text)
        return False


def delete_cname(container_name: str) -> bool:
    """Delete the CNAME record for container_name.domain if it exists."""
    fqdn = f"{container_name.lower()}.{DOMAIN}"
    record = _find_record(fqdn)
    if not record:
        log.debug("No CNAME found for %s, nothing to delete.", fqdn)
        return True

    record_id = record.get("_id") or record.get("id")
    if not record_id:
        log.error("Could not determine record ID for %s", fqdn)
        return False

    r = requests.delete(
        f"{BASE_URL}/{record_id}", headers=HEADERS, verify=False, timeout=10
    )
    if r.ok:
        log.info("Deleted CNAME: %s", fqdn)
        return True
    else:
        log.error("Failed to delete CNAME %s: %s %s", fqdn, r.status_code, r.text)
        return False


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------

def get_container_ip(container) -> str | None:
    """
    Return the container's IP if it has its own macvlan/ipvlan IP.
    Returns None for containers running on a shared bridge/host IP.
    """
    networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
    for net_name, net_info in networks.items():
        if net_name in SKIP_NETWORKS:
            continue
        ip = net_info.get("IPAddress")
        if ip:
            return ip
    return None


def should_skip(name: str) -> bool:
    """Return True if this container should be ignored."""
    if name in SKIP_CONTAINERS:
        log.debug("Container %s is in SKIP_CONTAINERS, ignoring.", name)
        return True
    # Skip the sync container itself
    if "unifi-dns-sync" in name:
        return True
    return False


# ---------------------------------------------------------------------------
# Main event loop
# ---------------------------------------------------------------------------

def event_loop(client):
    """Blocking Docker event loop — runs in a separate thread."""
    log.info("Listening for Docker events...")
    try:
        for event in client.events(decode=True):
            if _shutdown:
                break

            if event.get("Type") != "container":
                continue

            status = event.get("status")
            if status not in ("start", "die", "stop"):
                continue

            attrs = event.get("Actor", {}).get("Attributes", {})
            name = attrs.get("name", "")

            if should_skip(name):
                continue

            # Re-inspect the container to get its current network info
            try:
                container = client.containers.get(name)
                ip = get_container_ip(container)
            except docker.errors.NotFound:
                ip = None

            if status == "start":
                if ip:
                    log.info("Container started with own IP: %s (%s)", name, ip)
                    create_cname(name)
                else:
                    log.debug("Container %s started but has no dedicated IP, skipping.", name)
            elif status in ("die", "stop"):
                log.info("Container stopped: %s", name)
                delete_cname(name)

    except Exception as exc:
        if not _shutdown:
            log.error("Event loop error: %s", exc)


def main():
    import threading

    if not UNIFI_API_KEY:
        raise SystemExit("ERROR: UNIFI_API_KEY environment variable is not set.")

    log.info("unifi-dns-sync starting up")
    log.info("  UniFi host  : %s (site: %s)", UNIFI_HOST, UNIFI_SITE)
    log.info("  Domain      : %s", DOMAIN)
    log.info("  CNAME target: %s", NPM_CNAME_TARGET)

    client = docker.from_env()

    # On startup, sync all currently running containers that have their own IP
    log.info("Syncing already-running containers...")
    for container in client.containers.list():
        name = container.name
        if should_skip(name):
            continue
        ip = get_container_ip(container)
        if ip:
            log.info("Found running container with own IP: %s (%s)", name, ip)
            create_cname(name)
        else:
            log.debug("Container %s has no dedicated IP, skipping.", name)

    # Run the blocking event loop in a daemon thread so signals reach main
    t = threading.Thread(target=event_loop, args=(client,), daemon=True)
    t.start()

    # Main thread just waits for shutdown signal
    while not _shutdown:
        time.sleep(0.5)

    log.info("Shutdown complete.")
    sys.exit(0)


if __name__ == "__main__":
    while not _shutdown:
        try:
            main()
        except SystemExit:
            raise
        except Exception as exc:
            if _shutdown:
                break
            log.error("Unexpected error: %s — restarting in 10s", exc)
            time.sleep(10)
