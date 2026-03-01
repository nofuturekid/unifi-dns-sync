# unifi-dns-sync

<p align="center">
  <img src="icon.png" width="128" alt="unifi-dns-sync icon" />
</p>

<p align="center">
  <a href="https://github.com/nofuturekid/unifi-dns-sync/actions/workflows/build.yml">
    <img src="https://github.com/nofuturekid/unifi-dns-sync/actions/workflows/build.yml/badge.svg" alt="Build" />
  </a>
  <a href="https://ghcr.io/nofuturekid/unifi-dns-sync">
    <img src="https://img.shields.io/badge/ghcr.io-nofuturekid%2Funifi--dns--sync-blue?logo=github" alt="Container Registry" />
  </a>
  <img src="https://img.shields.io/badge/unRAID-compatible-orange" alt="unRAID" />
</p>

Automatically creates, updates and deletes **UniFi local DNS records** when Docker containers start or stop on unRAID. Inspired by Traefik's label-based configuration — opt in per container, configure per label.

---

## How it works

```
Container starts  →  dns.unifi.enable=true?  →  Create/update DNS record in UniFi
Container stops   →  dns.unifi.enable=true?  →  Delete DNS record from UniFi
```

- **Opt-in only** — containers without `dns.unifi.enable=true` are ignored
- **CNAME** (default) — points to a reverse proxy like Nginx Proxy Manager
- **A record** — points to a fixed or auto-detected container IP
- **Multiple hostnames** per container supported
- **On startup** — all already-running opted-in containers are synced automatically

---

## Prerequisites

1. **UniFi Gateway** with Network API support (UnifiOS 4.x+, Network 9.x+)
2. **UniFi API Key** — create one in:
   `UniFi → Settings → Control Plane → Integrations`
3. For CNAME records: a **proxy A-record** in UniFi DNS pointing to your reverse proxy:
   `npm.home.example.com  →  A  →  <IP of your proxy>`

---

## Installation

### unRAID (recommended)

```bash
wget -O /boot/config/plugins/dockerMan/templates-user/unifi_dns_sync.xml \
  https://raw.githubusercontent.com/nofuturekid/unifi-dns-sync/main/unifi_dns_sync.xml
```

Then go to **Docker → Add Container** and select the `unifi-dns-sync` template.

### Docker Compose

```yaml
services:
  unifi-dns-sync:
    image: ghcr.io/nofuturekid/unifi-dns-sync:latest
    container_name: unifi-dns-sync
    restart: unless-stopped
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    environment:
      UNIFI_HOST: "https://192.168.1.1"
      UNIFI_API_KEY: "your-api-key-here"
      DOMAIN: "home.example.com"
      DNS_DEFAULT_TYPE: "CNAME"
      DNS_DEFAULT_TARGET: "npm.home.example.com"
```

---

## Global configuration

Set via environment variables (or the unRAID template).

| Variable | Default | Required | Description |
|---|---|---|---|
| `UNIFI_HOST` | `https://192.168.1.1` | ✅ | URL of your UniFi Gateway |
| `UNIFI_API_KEY` | — | ✅ | UniFi API Key |
| `UNIFI_SITE` | `default` | | UniFi site name |
| `DOMAIN` | — | ✅ | Internal domain (e.g. `home.example.com`) |
| `DNS_DEFAULT_TYPE` | `CNAME` | | Default record type (`CNAME` or `A`) |
| `DNS_DEFAULT_TARGET` | — | ✅ for CNAME | Default CNAME target (e.g. `npm.home.example.com`) |
| `LOG_LEVEL` | `INFO` | | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

---

## Per-container labels

| Label | Required | Default | Description |
|---|---|---|---|
| `dns.unifi.enable` | ✅ | — | Set to `true` to enable DNS management |
| `dns.unifi.hostname` | | Container name | Subdomain(s), comma-separated |
| `dns.unifi.type` | | `DNS_DEFAULT_TYPE` | Record type: `CNAME` or `A` |
| `dns.unifi.target` | | `DNS_DEFAULT_TARGET` | CNAME target, explicit IP, or `auto` |

### dns.unifi.target values

| Value | Type | Behaviour |
|---|---|---|
| *(not set)* | `CNAME` | Uses `DNS_DEFAULT_TARGET` from global config |
| `npm.home.example.com` | `CNAME` | Uses this value as CNAME target |
| *(not set)* | `A` | ❌ Error — must be set explicitly or `auto` |
| `auto` | `A` | Auto-detects container IP (macvlan/ipvlan) |
| `192.168.1.50` | `A` | Uses this explicit IP address |

---

## Valid label combinations

| `type` | `hostname` | `target` | Container IP | Result |
|---|---|---|---|---|
| *(not set)* | *(not set)* | *(not set)* | any | CNAME: `<container-name>.domain` → `DNS_DEFAULT_TARGET` |
| *(not set)* | `plex` | *(not set)* | any | CNAME: `plex.domain` → `DNS_DEFAULT_TARGET` |
| *(not set)* | `plex,media` | *(not set)* | any | CNAME: `plex.domain` + `media.domain` → `DNS_DEFAULT_TARGET` |
| `CNAME` | *(not set)* | `proxy.example.com` | any | CNAME: `<container-name>.domain` → `proxy.example.com` |
| `A` | *(not set)* | `auto` | available | A: `<container-name>.domain` → container IP |
| `A` | *(not set)* | `auto` | **not available** | ❌ Error — no usable IP found |
| `A` | *(not set)* | `192.168.1.50` | any | A: `<container-name>.domain` → `192.168.1.50` |
| `A` | *(not set)* | *(not set)* | any | ❌ Error — target required for type=A |

---

## Examples

### Minimal — CNAME via global default

```
--label dns.unifi.enable=true
```

Result: `plex.home.example.com  CNAME  →  npm.home.example.com`

---

### Custom hostname

```
--label dns.unifi.enable=true
--label dns.unifi.hostname=mediaserver
```

Result: `mediaserver.home.example.com  CNAME  →  npm.home.example.com`

---

### Multiple hostnames

```
--label dns.unifi.enable=true
--label dns.unifi.hostname=plex,mediaserver
```

Result:
```
plex.home.example.com        CNAME  →  npm.home.example.com
mediaserver.home.example.com CNAME  →  npm.home.example.com
```

---

### Custom CNAME target (different proxy)

```
--label dns.unifi.enable=true
--label dns.unifi.target=other-proxy.home.example.com
```

Result: `<container-name>.home.example.com  CNAME  →  other-proxy.home.example.com`

---

### A record — auto IP (macvlan container)

```
--label dns.unifi.enable=true
--label dns.unifi.type=A
--label dns.unifi.target=auto
```

Result: `<container-name>.home.example.com  A  →  <container IP>`

---

### A record — explicit IP

```
--label dns.unifi.enable=true
--label dns.unifi.type=A
--label dns.unifi.target=192.168.1.50
```

Result: `<container-name>.home.example.com  A  →  192.168.1.50`

---

### Docker Compose example

```yaml
services:
  plex:
    image: plexinc/pms-docker
    labels:
      dns.unifi.enable: "true"
      dns.unifi.hostname: "plex,mediaserver"
      dns.unifi.type: "CNAME"
```

---

## Container naming

Container names are automatically lowercased before being used as subdomains:

| Container name | DNS entry |
|---|---|
| `plex` | `plex.home.example.com` |
| `Sabnzbd` | `sabnzbd.home.example.com` |
| `MQTT` | `mqtt.home.example.com` |

---

## Troubleshooting

```bash
# View live logs
docker logs -f unifi-dns-sync

# Test UniFi API connectivity
curl -k -X GET 'https://192.168.1.1/proxy/network/v2/api/site/default/static-dns' \
  -H 'X-API-KEY: your-api-key' \
  -H 'Accept: application/json'
```

---

## License

MIT — see [LICENSE](LICENSE)
