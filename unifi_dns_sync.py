#!/usr/bin/env python3
"""
unifi-dns-sync: Listens for Docker container start/stop/die events and
automatically creates/deletes DNS records in UniFi's local DNS.

Opt-in via Docker labels:
  dns.unifi.enable=true          Required — enables DNS management for this container
  dns.unifi.hostname=plex        Optional — subdomain(s), comma-separated
  dns.unifi.type=CNAME           Optional — record type: CNAME (default) or A
  dns.unifi.target=auto          Optional — CNAME target or A record IP

See README.md for full label reference and valid combinations.
"""

import os
import signal
import sys
import time
import logging
import threading
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
# Configuration — override via environment variables
# ---------------------------------------------------------------------------
UNIFI_HOST          = os.getenv("UNIFI_HOST", "https://192.168.1.1")
UNIFI_API_KEY       = os.getenv("UNIFI_API_KEY", "")
UNIFI_SITE          = os.getenv("UNIFI_SITE", "default")
DOMAIN              = os.getenv("DOMAIN", "home.example.com")
DNS_DEFAULT_TYPE    = os.getenv("DNS_DEFAULT_TYPE", "CNAME").upper()
DNS_DEFAULT_TARGET  = os.getenv("DNS_DEFAULT_TARGET", "")
LOG_LEVEL           = os.getenv("LOG_LEVEL", "INFO").upper()

# Docker networks to skip when auto-detecting container IPs (type=A, target=auto)
IP_LOOKUP_SKIP_NETWORKS = {"bridge", "host", "none"}

# Label keys
LABEL_ENABLE   = "dns.unifi.enable"
LABEL_HOSTNAME = "dns.unifi.hostname"
LABEL_TYPE     = "dns.unifi.type"
LABEL_TARGET   = "dns.unifi.target"

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
    """Fetch all static DNS records from UniFi."""
    r = requests.get(BASE_URL, headers=HEADERS, verify=False, timeout=10)
    r.raise_for_status()
    return r.json()


def _find_record(fqdn: str, record_type: str) -> dict | None:
    """Find an existing DNS record by FQDN and type. Returns the record or None."""
    for record in _get_all_records():
        if record.get("key") == fqdn and record.get("record_type") == record_type:
            return record
    return None


def create_or_update_record(fqdn: str, record_type: str, value: str) -> bool:
    """
    Create a DNS record, or update it if it already exists with a different value.
    Used for both CNAME and A records.
    """
    existing = _find_record(fqdn, record_type)

    if existing:
        if existing.get("value") == value:
            log.info("%s %s already exists with correct value, skipping.", record_type, fqdn)
            return True
        # Value has changed (e.g. dynamic IP) — update via PUT
        record_id = existing.get("_id") or existing.get("id")
        payload = {"key": fqdn, "record_type": record_type, "value": value, "enabled": True}
        r = requests.put(
            f"{BASE_URL}/{record_id}", headers=HEADERS, json=payload, verify=False, timeout=10
        )
        if r.ok:
            log.info("Updated %s: %s -> %s", record_type, fqdn, value)
            return True
        else:
            log.error("Failed to update %s %s: %s %s", record_type, fqdn, r.status_code, r.text)
            return False

    # Record does not exist yet — create via POST
    payload = {"key": fqdn, "record_type": record_type, "value": value, "enabled": True}
    r = requests.post(BASE_URL, headers=HEADERS, json=payload, verify=False, timeout=10)
    if r.ok:
        log.info("Created %s: %s -> %s", record_type, fqdn, value)
        return True
    else:
        log.error("Failed to create %s %s: %s %s", record_type, fqdn, r.status_code, r.text)
        return False


def delete_record(fqdn: str, record_type: str) -> bool:
    """Delete a DNS record by FQDN and type if it exists."""
    record = _find_record(fqdn, record_type)
    if not record:
        log.debug("No %s record found for %s, nothing to delete.", record_type, fqdn)
        return True

    record_id = record.get("_id") or record.get("id")
    if not record_id:
        log.error("Could not determine record ID for %s", fqdn)
        return False

    r = requests.delete(
        f"{BASE_URL}/{record_id}", headers=HEADERS, verify=False, timeout=10
    )
    if r.ok:
        log.info("Deleted %s: %s", record_type, fqdn)
        return True
    else:
        log.error("Failed to delete %s %s: %s %s", record_type, fqdn, r.status_code, r.text)
        return False


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------

def get_container_ip(container) -> str | None:
    """
    Return the container's first IP from a non-skipped network.
    Used for type=A with target=auto.
    Returns None if no usable IP is found.
    """
    networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
    for net_name, net_info in networks.items():
        if net_name in IP_LOOKUP_SKIP_NETWORKS:
            continue
        ip = net_info.get("IPAddress")
        if ip:
            return ip
    return None


def resolve_dns_config(container) -> list[dict] | None:
    """
    Read and validate DNS labels from a container.

    Returns a list of record configs (one per hostname), or None if the
    container has not opted in or the label configuration is invalid.

    Each record config is a dict with keys: fqdn, type, value
    """
    labels = container.labels or {}

    # Container must explicitly opt in
    if labels.get(LABEL_ENABLE, "").lower() != "true":
        return None

    # Determine record type — label overrides global default
    record_type = labels.get(LABEL_TYPE, DNS_DEFAULT_TYPE).upper()
    if record_type not in ("CNAME", "A"):
        log.error(
            "Container %s has invalid dns.unifi.type=%s (must be CNAME or A), skipping.",
            container.name, record_type,
        )
        return None

    # Resolve target value
    raw_target = labels.get(LABEL_TARGET, "").strip()

    if record_type == "CNAME":
        # CNAME: use label target, fall back to global default
        value = raw_target or DNS_DEFAULT_TARGET
        if not value:
            log.error(
                "Container %s: type=CNAME but no target set and DNS_DEFAULT_TARGET is empty, skipping.",
                container.name,
            )
            return None

    elif record_type == "A":
        if not raw_target:
            # target not set at all — error, must be explicit or 'auto'
            log.error(
                "Container %s: type=A requires dns.unifi.target=auto or an explicit IP, skipping.",
                container.name,
            )
            return None
        elif raw_target == "auto":
            # Auto-detect container IP
            value = get_container_ip(container)
            if not value:
                log.error(
                    "Container %s: type=A target=auto but no usable container IP found, skipping.",
                    container.name,
                )
                return None
        else:
            # Explicit IP address provided
            value = raw_target

    # Resolve hostnames — comma-separated, fall back to container name
    raw_hostnames = labels.get(LABEL_HOSTNAME, "").strip()
    hostnames = [h.strip() for h in raw_hostnames.split(",") if h.strip()] if raw_hostnames else [container.name]

    # Build one record config per hostname
    records = []
    for hostname in hostnames:
        fqdn = f"{hostname.lower()}.{DOMAIN}"
        records.append({"fqdn": fqdn, "type": record_type, "value": value})

    return records


def is_self(name: str) -> bool:
    """Prevent the sync container from managing DNS entries for itself."""
    return "unifi-dns-sync" in name


# ---------------------------------------------------------------------------
# Sync actions
# ---------------------------------------------------------------------------

def sync_container_start(container):
    """Handle container start — create or update DNS records."""
    configs = resolve_dns_config(container)
    if configs is None:
        log.debug("Container %s: no valid DNS config, skipping.", container.name)
        return
    for cfg in configs:
        create_or_update_record(cfg["fqdn"], cfg["type"], cfg["value"])


def sync_container_stop(container, event_attrs: dict):
    """
    Handle container stop/die — delete DNS records.
    Falls back to event attributes if the container is no longer inspectable.
    """
    if container is not None:
        configs = resolve_dns_config(container)
    else:
        # Container is gone — reconstruct minimal config from event labels
        enable = event_attrs.get(f"label.{LABEL_ENABLE}", "").lower()
        if enable != "true":
            return

        record_type = event_attrs.get(f"label.{LABEL_TYPE}", DNS_DEFAULT_TYPE).upper()
        name = event_attrs.get("name", "")
        raw_hostnames = event_attrs.get(f"label.{LABEL_HOSTNAME}", "").strip()
        hostnames = (
            [h.strip() for h in raw_hostnames.split(",") if h.strip()]
            if raw_hostnames else [name]
        )
        configs = [
            {"fqdn": f"{h.lower()}.{DOMAIN}", "type": record_type, "value": ""}
            for h in hostnames
        ]

    if not configs:
        return

    for cfg in configs:
        delete_record(cfg["fqdn"], cfg["type"])


# ---------------------------------------------------------------------------
# Main event loop
# ---------------------------------------------------------------------------

def event_loop(client):
    """Blocking Docker event loop — runs in a dedicated daemon thread."""
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
            name  = attrs.get("name", "")

            if is_self(name):
                continue

            try:
                container = client.containers.get(name)
            except docker.errors.NotFound:
                container = None

            if status == "start":
                if container is not None:
                    sync_container_start(container)
            elif status in ("die", "stop"):
                sync_container_stop(container, attrs)

    except Exception as exc:
        if not _shutdown:
            log.error("Event loop error: %s", exc)


def main():
    if not UNIFI_API_KEY:
        raise SystemExit("ERROR: UNIFI_API_KEY environment variable is not set.")

    log.info("unifi-dns-sync starting up")
    log.info("  UniFi host     : %s (site: %s)", UNIFI_HOST, UNIFI_SITE)
    log.info("  Domain         : %s", DOMAIN)
    log.info("  Default type   : %s", DNS_DEFAULT_TYPE)
    log.info("  Default target : %s", DNS_DEFAULT_TARGET or "(none)")

    client = docker.from_env()

    # On startup — sync all already-running opted-in containers
    log.info("Syncing already-running containers...")
    for container in client.containers.list():
        if is_self(container.name):
            continue
        sync_container_start(container)

    # Run the blocking event loop in a daemon thread so signals reach main
    t = threading.Thread(target=event_loop, args=(client,), daemon=True)
    t.start()

    # Main thread waits for shutdown signal
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
