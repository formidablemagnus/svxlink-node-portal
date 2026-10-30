# SvxLink Manager

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

## Versioning

The portal version is stored in the root `VERSION` file. The agent has an
independent version in `agent/VERSION`, which is reported by each node. When a
release tag is pushed, the version workflow updates the agent version to match
the tag only if agent code, its configuration helper, installer, or systemd
unit changed since the previous tag. Portal-only releases leave the agent
version unchanged. Tags containing `-dev` also update the agent version (when
the agent changed), but only on `develop`; the portal version is left as is.

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
The portal has a login page (`/login`); sessions last 12 hours and are kept in
an HttpOnly, SameSite=Strict cookie (Secure when the proxy sends
`X-Forwarded-Proto: https`). Usernames are case-insensitive. After 10 failed
logins for a username the login is blocked for 5 minutes. The portal
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

#### One-line install command

Under **Inställningar → Nodkonfig** an admin saves the portal URL, the GitHub
repository (`https://github.com/owner/repo`), branch/tag, SvxLink config and
log paths and the report interval. Once the portal URL is set, **Lägg till nod**
shows a ready-made command of the form
`curl -fsSL <raw install.sh> | sudo env AGENT_PORTAL_URL=… AGENT_NODE_ID=… AGENT_TOKEN=… bash -s -- agent`.
It clones the repository to a temporary directory and installs the agent
without prompts. The node needs `git`, `curl`, `sudo` and `python3`. The
command contains the token, so it ends up in shell history and the process
list; it is only shown at creation, and the token can be rotated by creating a
new node. Nodkonfig is not included in portal backups.

The same non-interactive mode works manually with the variables
`AGENT_PORTAL_URL`, `AGENT_NODE_ID`, `AGENT_TOKEN`, `AGENT_INTERVAL_SECONDS`,
`SVXLINK_CONFIG`, `SVXLINK_LOGFILE`, `SVX_INSTALL_REPO` and `SVX_INSTALL_REF`.

#### Interactive install

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
installer limits the `svxlink` account's sudo access to the fixed configuration
and SvxLink-update helpers, SvxLink service controls, and the crontab operations
used for schedules; reinstall or update the agent role to install these rules.

The log panel can be filtered by free-text search (optionally as a regular
expression) and by level (errors, warnings, other). Filtering happens in the
browser on the lines already reported by the node.

### Update SvxLink

On a node's detail page, an administrator can choose **Pre-release (master)**
or one of the four newest stable releases (the list shows their version
numbers) and start an update. Pre-release builds use the
latest `master` commit; stable builds use the chosen non-prerelease GitHub
release tag. Both build a Debian package and restart `svxlink.service` after
installation. The command and its result are recorded in the portal audit log.
The agent reports when the update starts and sends a progress heartbeat every
20 seconds. The node page shows the current status, disables node controls while
the update runs, and the dashboard displays a spinner beside the SvxLink
version. Builds can take several hours, so keep the node online and allow the
command to finish.

The installer creates a separate, non-login `svxlink-node-updater` account.
It keeps its source checkout and CMake build directory in
`/var/lib/svxlink-node-updater/svxlink`. On the first update it clones the
official `sm0svx/svxlink` repository and configures the build with Qt disabled
and Debian package generation enabled. The node needs internet access to
GitHub and the same SvxLink compiler, library and documentation dependencies
used for a source build (including `g++`, `cmake`, `make`, `git`, `curl`,
`doxygen` and `groff`). Missing build dependencies or a failed build are
reported in the node page; install the missing dependencies and retry.
For Debian 12/Raspberry Pi OS, the upstream source-build guide lists these
packages:

```bash
sudo apt install g++ cmake make libsigc++-2.0-dev libgsm1-dev libpopt-dev \
  tcl-dev libgcrypt20-dev libspeex-dev libasound2-dev libopus-dev librtlsdr-dev \
  doxygen groff alsa-utils vorbis-tools curl libcurl4-openssl-dev git rtl-sdr \
  libjsoncpp-dev libgpiod-dev libssl-dev ladspa-sdk
```

The build runs as the separate, unprivileged updater account, not as the node
agent. The installer grants the agent access to one root-owned helper with no
command-line arguments; it accepts only the two channel choices. A transient
systemd unit runs the fixed build workflow, validates the resulting SvxLink
package, installs it with `dpkg`, and restarts the fixed SvxLink service.
Installing a package executes its maintainer scripts as root, so only use this
feature when you trust the selected upstream SvxLink source.

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
(`/admin`) admins can add users (passwords of at least 8 characters with upper- and lower-case letters and digits, stored as
salted PBKDF2-SHA256 hashes), change passwords and roles, and delete users.
Role `operator` can view node status and logs and send DTMF sequences (including
the shared quick buttons) but cannot create or delete nodes, start/stop SvxLink,
edit configuration, node information, schedules or DTMF buttons, manage users, read the audit
log, or use backup/restore. Those controls are hidden for operators and the
server rejects the requests with 403. Role `admin` can do everything.

Every command sent to a node (DTMF sequence, service start/stop, SvxLink
updates, configuration read/write, schedule read/write), node creation and user changes are recorded
in the audit log with the acting user, time, node and outcome (queued,
completed, failed, expired). Configuration contents are never logged, only the
document name. The log keeps the latest 5000 entries; the DTMF sequences sent
are stored in clear text.

### Node service log

Every node has a separate **Servicelogg** page linked from its detail page.
Signed-in administrators and operators can add manual notes; the portal records
the current UTC timestamp and username automatically. Notes are kept with the
node and included in portal backups and restores.

### Edit node configuration

The node detail page links to two separate pages: a configuration page for
`/etc/svxlink/svxlink.conf` (`/nodes/<id>/config`) and a node information page
for `/etc/svxlink/node_info.json` (`/nodes/<id>/nodeinfo`, with a form editor
and validation plus a raw JSON view). Portal
administrators can read and edit both files. The node must be
online to read or process an edit request. Each file is loaded automatically
when its page opens. Each save creates a timestamped backup
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

### Remote operation over UDP audio

When a node's `svxlink.conf` uses `AUDIO_DEV=udp:<address>:<port>` for the
receiver and/or transmitter of an active logic, the agent reports this and the
node page shows **Fjärrdrift (UDP-ljud)**. Click **Anslut ljud** to listen in
the browser and hold **PTT** to transmit with your microphone (microphone
access requires HTTPS or `localhost`). Audio is relayed through the portal's
existing HTTP connection (16 kHz mono PCM), so no extra ports have to be opened
towards the portal. Only one user can transmit at a time and every transmission
start is written to the audit log.

```ini
[Rx1]
AUDIO_DEV=udp:127.0.0.1:10000   # audio from the portal into SvxLink
CARD_SAMPLE_RATE=48000
SQL_DET=PTY                     # PTT: the agent writes O (open) / Z (close)
SQL_PTY=/dev/shm/remote_ptt

[Tx1]
AUDIO_DEV=udp:127.0.0.1:10001   # audio from SvxLink to the agent
CARD_SAMPLE_RATE=48000
```

The `[Tx]` address must be local to the node (for example `127.0.0.1`), since
the agent receives the audio on that UDP port while a browser is connected. The
`[Rx]` address is where SvxLink listens. The agent reads `CARD_SAMPLE_RATE` and
`CARD_CHANNELS` from the `[Rx]`/`[Tx]` section, falling back to `[GLOBAL]`
(defaults: 16000 Hz, 2 channels). Set `AGENT_UDP_CHANNELS=1` or `2` in
`/etc/default/svxlink-node-agent` to override the channel count. PTT is
released automatically if the browser stops sending, and after 3 minutes of
continuous transmission. Without `SQL_DET=PTY` the audio is still delivered, so
a VOX squelch can be used instead. Behind a reverse proxy, disable response
buffering for `/api/nodes/*/audio` (the portal sends `X-Accel-Buffering: no`).

**UDP transport between agent and portal.** By default the agent exchanges
audio with the portal over HTTP, which stutters on high-latency or jittery
links. When the portal and node share a direct network (for example
Tailscale), enable the UDP transport by adding to
`/etc/default/svxlink-node-portal` and restarting the portal:

```
PORTAL_AUDIO_UDP_PORT=10001
```

Allow that UDP port in the portal's firewall. The agent learns the port from
the portal and sends to the host in `AGENT_PORTAL_URL`. If the URL points at a
reverse proxy, set `AGENT_AUDIO_UDP_HOST` (and optionally
`AGENT_AUDIO_UDP_PORT`) in `/etc/default/svxlink-node-agent` to the portal's
own address. `AGENT_AUDIO_UDP=off` disables UDP on a node. Packets are
authenticated with an HMAC derived from the node token but are not encrypted,
so use a private or encrypted network. If the portal does not answer, the
agent falls back to HTTP automatically and retries UDP after 10 minutes.

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

### Uninstall

```bash
sudo bash ./uninstall.sh portal    # or: agent | both
```

Run without an argument for an interactive menu. By default the portal
database and settings files are kept so a reinstall can continue where it
left off. Add `--purge-data` to also delete the database, portal settings and
the `svxlink-node-portal` system user (download a backup from the Admin page
first). Add `--yes` to skip the confirmation prompt. Removing the agent also
removes its settings, config helper and sudoers rule; SvxLink itself is not
touched.

## Status API

`POST /api/v1/agent/status` accepts a bearer-authenticated JSON report. The
portal validates and stores only service state, radio mode/signal, current
talkgroup, logic, callsign and a bounded snapshot of recent log lines. Reports
are considered online for 30 seconds.
`GET /api/nodes`, `GET /api/nodes/<node-id>`, the dashboard and node detail
pages require a portal login session. Select a node from the dashboard
to view its latest service, radio, signal, talkgroup, logic, callsign,
last-seen information and recent log lines. The agent tails the log,
reads `STATE_PTY` and polls the service continuously. Radio events and new log
lines are streamed to the portal at all times (`/api/v1/agent/radio` and
`/api/v1/agent/logs`) and kept in portal memory (last 200 lines per node), so
node pages load instantly. A full status report is sent every
`AGENT_INTERVAL_SECONDS` (default 30) and immediately on service, error or
talkgroup changes. After a portal restart the last full report is used until
new data arrives. The portal stores no long-term log archive.
Administrators can remove a node from the dashboard after confirming their
password. `DELETE /api/nodes/<node-id>` is admin-only and requires the password
in a JSON body; removal also clears queued agent commands and records an audit
event.

### Live radio stream

While a browser has a node detail page open (`GET /api/nodes/<id>/radio-events`,
Server-Sent Events, login session), the portal tells the agent to stream radio
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
