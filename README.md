# SvxLink Node Portal

Central dashboard and outbound node agent for managing multiple SvxLink nodes.
The first milestone reports service, radio, talkgroup, logic and callsign status;
remote control and editing are intentionally not enabled yet.

## Components

- `portal/` — central HTTP service, SQLite node registry and dashboard.
- `agent/` — lightweight agent that reads the local webpanel status and reports
  a validated status snapshot to the portal over HTTPS.
- `web/` — self-hosted dashboard assets; no external frontend dependencies.

The agent initiates every network request. A node does not need an inbound
firewall rule or a public address. Each node has a unique, randomly generated
bearer token; only its SHA-256 hash is stored in the portal database.

## Requirements

- Python 3.10 or later on the portal and nodes.
- The existing SvxLink Webpanel installed on each node.
- HTTPS for the portal endpoint, normally terminated by a reverse proxy.

The portal uses only Python's standard library. Bind it to loopback and put it
behind a TLS reverse proxy; do not expose its HTTP port to an untrusted network.
The dashboard and node-list API use HTTP Basic authentication. The portal
requires a password of at least 16 characters; protect the portal database and
environment file.

## Start the portal

Set portal credentials and a writable database path:

```bash
export PORTAL_ADMIN_USER=admin
export PORTAL_ADMIN_PASSWORD='replace-with-a-long-unique-password'
export PORTAL_DATABASE=/var/lib/svxlink-node-portal/portal.sqlite3
```

Create a node identity. The bearer token is printed once; copy it securely to
the matching node and do not put it in shell history or source control:

```bash
python3 -m portal.server provision-node --name "Repeater north"
```

Start the portal on loopback (the default):

```bash
python3 -m portal.server serve
```

The portal listens on `127.0.0.1:8080` by default. A reverse proxy should
provide HTTPS and proxy `/` and `/api/` to that address.

## Configure a node agent

Install this project's `agent/` package on a node, then provide the following
environment variables through a root-owned systemd environment file with mode
`0600`:

```bash
AGENT_PORTAL_URL=https://portal.example.net
AGENT_NODE_ID=<node_id from provision-node>
AGENT_TOKEN=<one-time token from provision-node>
AGENT_PANEL_URL=http://127.0.0.1:8090/api/status
AGENT_INTERVAL_SECONDS=5
SVXLINK_WEB_USER=<existing webpanel username>
SVXLINK_WEB_PASSWORD=<existing webpanel password>
SVXLINK_CONFIG=/etc/svxlink/svxlink.conf
```

Run the agent as an unprivileged service account that can read the local
configuration and reach the webpanel. The agent requires HTTPS for its portal
URL, verifies the server certificate using the system trust store, and reports
connection failures to its service log before retrying.

## Status API

`POST /api/v1/agent/status` accepts a bearer-authenticated JSON report. The
portal validates and stores only service state, radio mode/signal, current
talkgroup, logic and callsign. Reports are considered online for 30 seconds.
`GET /api/nodes` and the dashboard require portal Basic authentication.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Roadmap

1. Establish per-node registration and a central live status dashboard.
2. Add authenticated node detail pages and reuse the existing webpanel's
   read-only status and log views.
3. Add explicitly authorized remote controls, configuration editing,
   auditing and portal user management.
4. Package the agent and portal as hardened systemd services with documented
   upgrades and compatibility checks.
