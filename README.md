# SvxLink Node Portal

Central dashboard and outbound node agent for managing multiple SvxLink nodes.
The first milestone reports service, radio, talkgroup, logic and callsign status;
remote control and editing are intentionally not enabled yet.

## Components

- `portal/` — central HTTP service, SQLite node registry and dashboard.
- `agent/` — lightweight agent that reads the local webpanel status and reports
  a validated status snapshot to the portal over HTTP or HTTPS.
- `web/` — self-hosted dashboard assets; no external frontend dependencies.
- `install.sh` and `systemd/` — role-based installer and service units.

The agent initiates every network request. A node does not need an inbound
firewall rule or a public address. Each node has a unique, randomly generated
bearer token; only its SHA-256 hash is stored in the portal database.

## Requirements

- Linux with systemd for the installer and services.
- Python 3.10 or later on the portal and nodes.
- Git on the portal server and nodes.
- The existing SvxLink Webpanel installed on each node.
- HTTPS is recommended for the portal endpoint. HTTP is also supported for
  trusted private networks.

The portal uses only Python's standard library. Bind it to loopback and put it
behind a TLS reverse proxy; do not expose its HTTP port to an untrusted network.
The dashboard and node-list API use HTTP Basic authentication. The portal
requires a password of at least 16 characters; protect the portal database and
environment file.

## Install with systemd

The installer follows the same workflow as the SvxLink webpanel: choose the
portal or node-agent role, answer the configuration prompts, and let the
installer create, enable, and start the systemd service. The portal runs as
the separate `svxlink-node-portal` system account; the agent runs as the
existing `svxlink` account. Settings are stored under `/etc/default/` with
root-only permissions, and the portal database is stored under
`/var/lib/svxlink-node-portal/`.

### Get the source

Install Git and Python 3, then clone the repository using an account that can
access the private GitHub repository:

Authenticate GitHub CLI with `gh auth setup-git` or configure SSH access before
cloning. Never put an access token in the clone URL.

```bash
sudo apt update
sudo apt install -y git python3
git clone https://github.com/formidablemagnus/svxlink-node-portal.git
cd svxlink-node-portal
```

### Install the central portal

On the central Linux server, run:

```bash
sudo bash ./install.sh portal
```

The installer prompts for the portal credentials, installs
`svxlink-node-portal.service`, and starts it automatically. By default, the
service listens on `127.0.0.1:8080`; configure a TLS-terminating reverse
proxy in front of it and do not expose the HTTP port to untrusted networks.

Next, create an identity for each node. The command prints the node's bearer
token once; transfer it securely and do not save it in shell history or source
code:

```bash
sudo -u svxlink-node-portal sh -c \
  'cd /opt/svxlink-node-portal/app && exec python3 -m portal.server provision-node --name "Repeater north"'
```

### Install the node agent

On each SvxLink node, clone the repository (or make an existing clone
available) and run:

```bash
sudo bash ./install.sh agent
```

The installer prompts for the portal HTTP or HTTPS URL, node ID and one-time
token from `provision-node`, and the webpanel credentials. SvxLink and the
webpanel must already be installed; the agent runs as `svxlink`. The agent
initiates all connections, so no inbound firewall rule is needed.

HTTPS is strongly recommended. HTTP sends the bearer token and status reports
without encryption, so only use it on a trusted private network. The portal
defaults to listening on `127.0.0.1:8080`; for direct HTTP from nodes, set
`PORTAL_HOST` in `/etc/default/svxlink-node-portal` to an address reachable by
those nodes (for example `0.0.0.0`) and restrict port 8080 with the server
firewall to trusted node addresses. Restart the portal service after changing
its settings.

Use `portal` or `agent` when installing on separate servers. If both roles
should run on the same machine, use `sudo bash ./install.sh both` or run the
installer interactively without an argument.

### Check, configure, and update

```bash
sudo systemctl status svxlink-node-portal.service
sudo journalctl -u svxlink-node-portal.service -n 50 --no-pager
sudo systemctl status svxlink-node-agent.service
sudo journalctl -u svxlink-node-agent.service -n 50 --no-pager
```

Edit portal settings in `/etc/default/svxlink-node-portal` or agent settings
in `/etc/default/svxlink-node-agent`, then restart the corresponding service.
These files contain passwords and tokens and are installed with `root:root`,
`0600` permissions.

To update, fetch and review changes in the checkout, then run the installer
again with the same role. The installer updates the program and systemd unit
and restarts the service; settings and the portal database are preserved:

```bash
git pull --ff-only
sudo bash ./install.sh portal
```

Use `agent` on the nodes, or `both` if both roles are installed on the same
machine. For controlled deployments, check out a reviewed commit instead of
following the latest `main`.

## Status API

`POST /api/v1/agent/status` accepts a bearer-authenticated JSON report. The
portal validates and stores only service state, radio mode/signal, current
talkgroup, logic and callsign. Reports are considered online for 30 seconds.
`GET /api/nodes`, `GET /api/nodes/<node-id>`, the dashboard and node detail
pages require portal Basic authentication. Select a node from the dashboard
to view its latest service, radio, signal, talkgroup, logic, callsign and
last-seen information. Log lines are not yet forwarded from the webpanel.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Roadmap

1. Extend node detail pages with read-only status and log views from the
   existing webpanel.
2. Add explicitly authorized remote controls, configuration editing,
   auditing and portal user management.
3. Add documented compatibility checks and a guided reverse-proxy setup.
