# SvxLink Node Portal

Central dashboard and outbound node agent for managing multiple SvxLink nodes.
The portal reports service, radio, talkgroup, logic and callsign status, lets
portal administrators start or stop SvxLink, edit node configuration, and send
DTMF sequences to individual or selected online nodes.

## Components

- `portal/` — central HTTP service, SQLite node registry and dashboard.
- `agent/` — lightweight agent that collects all local node information itself
  (systemd service state, `svxlink.conf`, the SvxLink log and `STATE_PTY`) and
  reports it to the portal over HTTP or HTTPS. No webpanel is needed.
- `web/` — self-hosted dashboard assets; no external frontend dependencies.
- `install.sh` and `systemd/` — role-based installer and service units.

The agent initiates every network request. A node does not need an inbound
firewall rule or a public address. Each node has a unique, randomly generated
bearer token; only its SHA-256 hash is stored in the portal database.

## Requirements

- Linux with systemd for the installer and services.
- Python 3.10 or later on the portal and nodes.
- Git on the portal server and nodes.
- SvxLink installed on each node (the SvxLink Webpanel is not required).
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

Next, create an identity for each node from **Lägg till nod** in the portal
dashboard. The node ID and bearer token are shown after creation; the token is
shown only once. Transfer it securely and enter both values when installing
the agent on the node. Do not save the token in shell history or source code.
The command-line alternative prints the token once as well:

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
token from `provision-node`, and the SvxLink config and log paths. SvxLink
must already be installed; the agent runs as `svxlink` and needs read access to
the log file and the `STATE_PTY` configured in `svxlink.conf`. The agent
initiates all connections, so no inbound firewall rule is needed.

HTTPS is strongly recommended. HTTP sends the bearer token and status reports
without encryption, so only use it on a trusted private network. The portal
defaults to listening on `127.0.0.1:8080`; for direct HTTP from nodes, set
`PORTAL_HOST` in `/etc/default/svxlink-node-portal` to an address reachable by
those nodes (for example `0.0.0.0`) and restrict port 8080 with the server
firewall to trusted node addresses. Restart the portal service after changing
its settings.

### Control SvxLink and view logs

On an online node's detail page, portal administrators can start or stop the
`svxlink.service`. The latest log lines from the node are shown first. The agent
installer grants the `svxlink` account sudo access only to the fixed
configuration helper and `systemctl start/stop svxlink.service`; reinstall or
update the agent role to install this rule.

The log panel can be filtered by free-text search (optionally as a regular
expression) and by level (errors, warnings, other). Filtering happens in the
browser on the lines already reported by the node.

### Node information

The agent reads `node_info.json` from the same directory as `svxlink.conf`
(it must be readable by the `svxlink` account) and reports a bounded,
display-only summary: location, class, sysop, tone-to-talkgroup mapping and, per
QTH, position, receivers and transmitters (frequency, squelch, CTCSS, power,
antennas). The node detail page shows it above the log. Trailing commas in the
file are tolerated for display. If the file is missing or unreadable the panel
is hidden.

### Portal users and command audit

The administrator from `PORTAL_ADMIN_USER`/`PORTAL_ADMIN_PASSWORD` always
exists and is the bootstrap `admin`. Under **Användare & granskning**
(`/admin`) admins can add users (passwords of at least 12 characters, stored as
salted PBKDF2-SHA256 hashes), change passwords and roles, and delete users.
Role `operator` can control nodes (DTMF, service, configuration, schedules) but
cannot manage users or read the audit log; role `admin` can do both.

Every command sent to a node (DTMF sequence, service start/stop, configuration
read/write, schedule read/write), node creation and user changes are recorded
in the audit log with the acting user, time, node and outcome (queued,
completed, failed, expired). Configuration contents are never logged, only the
document name. The log keeps the latest 5000 entries; the DTMF sequences sent
are stored in clear text.

### Edit node configuration

The node detail page links to a separate configuration page. Portal
administrators can read and edit
`/etc/svxlink/svxlink.conf` and `/etc/svxlink/node_info.json`. The node must be
online to read or process an edit request. Both files are loaded automatically
when the configuration page opens. Each save creates a timestamped backup
beside the original file and preserves its owner and mode. A checksum check
prevents saving if the file changed after it was loaded; reload it and reapply
your edits in that case. `node_info.json` must remain valid JSON. Saving does
not restart SvxLink; restart it manually when required.

The agent installer installs a root-owned helper limited to those two fixed
paths and the sudoers rule described above. The agent systemd service therefore
allows these controlled privilege elevations. Keep portal access restricted to
trusted administrators and prefer HTTPS: an authorized portal user can change
the node's SvxLink configuration or control its service. Configuration contents
pass through the portal database while commands are processed, so keep that
database accessible only to the portal service account and system
administrators.

### Send DTMF commands

The node detail page has a DTMF control for one node. Use **DTMF-styrning** in
the dashboard to select multiple online nodes and send the same sequence to
each; the portal shows delivery status separately for every selected node.
Sequences may contain `0`–`9`, `A`–`D`, `*` and `#`, and are sent exactly as
entered. Add `#` when the SvxLink command should be executed.

Preset buttons ("Snabbknappar") are global for all nodes and stored in the
portal database (up to 30, name 1–40 characters). Create and delete them on the
**DTMF-styrning** page. On a node's detail page a button sends its sequence to
that node; on the multi-node page it sends to the selected nodes after a
confirmation.

For DTMF control, configure `DTMF_CTRL_PTY` in each SvxLink logic section that
should receive commands, for example:

```ini
[RepeaterLogic]
DTMF_CTRL_PTY=/run/svxlink/repeater_dtmf_ctrl
```

Use the actual path configured for your installation. The path must be an
existing PTY or FIFO writable by the `svxlink` account, which also runs the
portal node agent. Restart SvxLink after changing its configuration. If no
`DTMF_CTRL_PTY` is configured for an active logic, that node's DTMF request
will fail with an explanatory result. Nodes must be online and their agent
must be polling the portal for queued commands to be delivered.

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
talkgroup, logic, callsign and a bounded snapshot of recent log lines. Reports
are considered online for 30 seconds.
`GET /api/nodes`, `GET /api/nodes/<node-id>`, the dashboard and node detail
pages require portal Basic authentication. Select a node from the dashboard
to view its latest service, radio, signal, talkgroup, logic, callsign,
last-seen information and up to 50 recent log lines. The agent tails the log,
reads `STATE_PTY` and polls the service continuously; it sends a status report
immediately on changes (and at least every `AGENT_INTERVAL_SECONDS`, default 5).
The portal stores only the latest report, not a long-term log archive.
Administrators can remove a node from the dashboard after confirming their
password. `DELETE /api/nodes/<node-id>` is admin-only and requires the password
in a JSON body; removal also clears queued agent commands and records an audit
event.

### Live radio stream

While a browser has a node detail page open (`GET /api/nodes/<id>/radio-events`,
Server-Sent Events, Basic auth), the portal tells the agent to stream radio
events (`POST /api/v1/agent/radio`: mode, signal level, per-receiver squelch
and signal, transmitter state). The agent learns this from the `watch` flag in
its command-poll (default every second, `AGENT_COMMAND_POLL_SECONDS`) and
status responses, and stops streaming when nobody is watching. Radio events
are kept only in memory.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Roadmap

1. Add documented compatibility checks and a guided reverse-proxy setup.
