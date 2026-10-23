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
- Git on the portal server and nodes.
- The existing SvxLink Webpanel installed on each node.
- HTTPS for the portal endpoint, normally terminated by a reverse proxy.

The portal uses only Python's standard library. Bind it to loopback and put it
behind a TLS reverse proxy; do not expose its HTTP port to an untrusted network.
The dashboard and node-list API use HTTP Basic authentication. The portal
requires a password of at least 16 characters; protect the portal database and
environment file.

## Install and run the portal from Git

On the central Linux server, clone the repository and install Python:

```bash
sudo apt update
sudo apt install -y git python3
sudo install -d -o "$USER" -g "$USER" -m 0755 /opt/svxlink-node-portal
git clone https://github.com/formidablemagnus/svxlink-node-portal.git \
  /opt/svxlink-node-portal
cd /opt/svxlink-node-portal
```

The repository is private. Make sure the account running `git clone` has
permission to access it, for example by authenticating with GitHub CLI first.
If you use GitHub CLI for HTTPS authentication, run `gh auth setup-git` first.
Alternatively, configure SSH access for the account that will clone the repo.
Do not put a GitHub access token in the clone URL.

Create a private environment file for portal settings. Replace the example
password with a unique value of at least 16 characters:

```bash
sudo install -o root -g root -m 0600 /dev/null /etc/default/svxlink-node-portal
sudoedit /etc/default/svxlink-node-portal
sudo chown "$USER":"$USER" /etc/default/svxlink-node-portal
```

Add these shell-compatible settings to the file:

```bash
PORTAL_ADMIN_USER=admin
PORTAL_ADMIN_PASSWORD='replace-with-a-long-unique-password'
PORTAL_DATABASE=/var/lib/svxlink-node-portal/portal.sqlite3
PORTAL_HOST=127.0.0.1
PORTAL_PORT=8080
```

The commands below run the portal as your current user, so give that user
ownership of its database directory:

```bash
sudo install -d -o "$USER" -g "$USER" -m 0750 /var/lib/svxlink-node-portal
```

Load the settings and start the portal in the foreground:

```bash
set -a
. /etc/default/svxlink-node-portal
set +a
cd /opt/svxlink-node-portal
python3 -m portal.server serve
```

Keep the portal behind a TLS reverse proxy that forwards `/` and `/api/` to
`127.0.0.1:8080`. Do not expose the portal's plain HTTP listener to untrusted
networks. The current Git version does not include systemd service or reverse
proxy configuration; the command above runs in the foreground and stops when
the shell exits.

### Register a node

In a second shell on the portal server, load the same settings and create a
unique identity for each node:

```bash
set -a
. /etc/default/svxlink-node-portal
set +a
cd /opt/svxlink-node-portal
python3 -m portal.server provision-node --name "Repeater north"
```

The command prints the node ID and bearer token once. Transfer them securely to
that node; the portal stores only the token's SHA-256 hash. Never add node
tokens or portal passwords to source control or shell history.

## Install and run a node agent from Git

On each SvxLink node, clone the same repository and check out the version or
commit you intend to run:

```bash
sudo apt update
sudo apt install -y git python3
sudo install -d -o "$USER" -g "$USER" -m 0755 /opt/svxlink-node-portal
git clone https://github.com/formidablemagnus/svxlink-node-portal.git \
  /opt/svxlink-node-portal
cd /opt/svxlink-node-portal
```

The private repository must be accessible from the node for the clone. The
agent needs no inbound firewall rule; it makes outbound HTTPS connections to
the portal.

Create an environment file restricted to the agent account for the node's
credentials and settings:

```bash
sudo install -o root -g root -m 0600 /dev/null /etc/default/svxlink-node-agent
sudoedit /etc/default/svxlink-node-agent
sudo chown svxlink:svxlink /etc/default/svxlink-node-agent
```

Add the settings below, replacing the example values with the node ID and
one-time token from `provision-node`, your HTTPS portal URL, and that node's
existing webpanel credentials:

```bash
AGENT_PORTAL_URL=https://portal.example.net
AGENT_NODE_ID='node_id from provision-node'
AGENT_TOKEN='one-time token from provision-node'
AGENT_PANEL_URL=http://127.0.0.1:8090/api/status
AGENT_INTERVAL_SECONDS=5
SVXLINK_WEB_USER='existing webpanel username'
SVXLINK_WEB_PASSWORD='existing webpanel password'
SVXLINK_CONFIG=/etc/svxlink/svxlink.conf
```

The commands above make the environment file readable only by the `svxlink`
account. Run the agent as an unprivileged account that can read that file,
the SvxLink configuration and the local webpanel. For an initial foreground
test:

```bash
sudo -u svxlink sh -c \
  'set -a; . /etc/default/svxlink-node-agent; set +a; cd /opt/svxlink-node-portal; exec python3 -m agent.agent'
```

If you use a dedicated agent account instead, change the environment file
owner and grant that account only the required configuration and webpanel
access. The current Git version does not include a systemd unit; the command
above runs in the foreground and stops when interrupted.

The agent requires an HTTPS portal URL, verifies the server certificate using
the system trust store, and logs connection failures before retrying. Check
the portal dashboard after starting the agent; a reporting node should appear
online within a few seconds.

### Update portal or agent from Git

On the machine being updated, fetch and review the changes before restarting
the foreground process:

```bash
cd /opt/svxlink-node-portal
git fetch origin
git pull --ff-only
python3 -m unittest discover -s tests -v
```

Stop the running portal or agent with `Ctrl+C`, then start it again using the
commands above. The SQLite database and credential files are outside the Git
checkout and are not replaced by `git pull`. For more controlled deployments,
check out a specific reviewed commit instead of following the latest `main`.

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

1. Add authenticated node detail pages and reuse the existing webpanel's
   read-only status and log views.
2. Add explicitly authorized remote controls, configuration editing,
   auditing and portal user management.
3. Package the agent and portal as hardened systemd services with documented
   upgrades and compatibility checks.
