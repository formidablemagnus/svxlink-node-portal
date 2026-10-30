#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/svxlink-node-portal/app"
PORTAL_ENV="/etc/default/svxlink-node-portal"
AGENT_ENV="/etc/default/svxlink-node-agent"
SYSTEMD_DIR="/etc/systemd/system"

say_error() {
  printf 'Fel: %s\n' "$*" >&2
}

require_tty() {
  if [[ ! -r /dev/tty ]]; then
    say_error "Installationen behöver en interaktiv terminal för att fråga efter inställningar."
    exit 1
  fi
}

prompt_value() {
  local prompt="$1"
  local default_value="${2-}"
  local answer
  if [[ -n "$default_value" ]]; then
    printf '%s [%s]: ' "$prompt" "$default_value" >/dev/tty
  else
    printf '%s: ' "$prompt" >/dev/tty
  fi
  IFS= read -r answer </dev/tty
  REPLY="${answer:-$default_value}"
}

prompt_secret() {
  local prompt="$1"
  printf '%s: ' "$prompt" >/dev/tty
  IFS= read -r -s REPLY </dev/tty
  printf '\n' >/dev/tty
}

systemd_quote() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '"%s"' "$value"
}

write_environment() {
  local file="$1"
  shift
  umask 077
  {
    printf '# Managed by the SvxLink Manager installer.\n'
    while (( $# )); do
      printf '%s=%s\n' "$1" "$(systemd_quote "$2")"
      shift 2
    done
  } > "$file"
  chown root:root "$file"
  chmod 0600 "$file"
}

install_component() {
  local component="$1"
  local source="$SCRIPT_DIR/$component"
  local target="$APP_DIR/$component"
  if [[ ! -d "$source" ]]; then
    say_error "Källkatalogen saknas: $source"
    exit 1
  fi
  install -d -o root -g root -m 0755 "$target"
  cp -a "$source/." "$target/"
  find "$target" -type d -name __pycache__ -prune -exec rm -rf -- {} +
  find "$target" -type f -name '*.pyc' -delete
  chown -R root:root "$target"
  find "$target" -type d -exec chmod 0755 {} +
  find "$target" -type f -exec chmod 0644 {} +
}

install_service() {
  local name="$1"
  local source="$SCRIPT_DIR/systemd/$name"
  if [[ ! -f "$source" ]]; then
    say_error "Systemd-enheten saknas: $source"
    exit 1
  fi
  install -o root -g root -m 0644 "$source" "$SYSTEMD_DIR/$name"
}

install_agent_config_helper() {
  local source="$SCRIPT_DIR/config_helper.py"
  local helper="/usr/local/libexec/svxlink-node-portal-config"
  local sudoers="/etc/sudoers.d/svxlink-node-portal-agent"
  local systemctl="/usr/bin/systemctl"
  if [[ ! -f "$source" ]]; then
    say_error "Konfigurationshjälpen saknas: $source"
    exit 1
  fi
  if [[ ! -x "$systemctl" ]]; then
    say_error "systemctl saknas på den förväntade platsen: $systemctl"
    exit 1
  fi
  if ! command -v visudo >/dev/null 2>&1; then
    say_error "visudo saknas. Installera sudo innan du installerar nodagenten."
    exit 1
  fi
  install -d -o root -g root -m 0755 /usr/local/libexec
  install -o root -g root -m 0755 "$source" "$helper"
  local sudoers_tmp
  sudoers_tmp="$(mktemp /etc/sudoers.d/svxlink-node-portal-agent.XXXXXX)"
  trap 'rm -f -- "$sudoers_tmp"' EXIT
  printf '%s\n' \
    'svxlink ALL=(root) NOPASSWD: /usr/local/libexec/svxlink-node-portal-config, /usr/bin/systemctl start svxlink.service, /usr/bin/systemctl stop svxlink.service, /usr/bin/systemctl restart svxlink.service, /usr/bin/crontab -u svxlink -l, /usr/bin/crontab -u svxlink -, /usr/bin/crontab -u svxlink -r' \
    > "$sudoers_tmp"
  chmod 0440 "$sudoers_tmp"
  if ! visudo -cf "$sudoers_tmp"; then
    rm -f "$sudoers_tmp"
    say_error "Sudoers-regeln för konfigurationshjälpen är ogiltig."
    exit 1
  fi
  mv "$sudoers_tmp" "$sudoers"
  trap - EXIT
}

install_agent_update_helpers() {
  local builder_source="$SCRIPT_DIR/agent/update.sh"
  local helper_source="$SCRIPT_DIR/agent/update_helper.py"
  local builder="/usr/local/libexec/svxlink-node-portal-build-update"
  local helper="/usr/local/libexec/svxlink-node-portal-update"
  local sudoers="/etc/sudoers.d/svxlink-node-portal-agent"
  local source
  for source in "$builder_source" "$helper_source"; do
    if [[ ! -f "$source" ]]; then
      say_error "Uppdateringshjälpen saknas: $source"
      exit 1
    fi
  done
  for executable in /usr/bin/dpkg /usr/bin/dpkg-deb /usr/bin/git /usr/bin/systemctl /usr/bin/systemd-run /usr/sbin/runuser; do
    if [[ ! -x "$executable" ]]; then
      say_error "Nödvändigt program saknas: $executable"
      exit 1
    fi
  done
  if ! id svxlink-node-updater >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /var/lib/svxlink-node-updater \
      --create-home --shell /usr/sbin/nologin svxlink-node-updater
  elif ! getent group svxlink-node-updater >/dev/null 2>&1; then
    say_error "Användaren svxlink-node-updater saknar en grupp med samma namn."
    exit 1
  fi
  install -d -o root -g root -m 0755 /usr/local/libexec
  install -o root -g root -m 0755 "$builder_source" "$builder"
  install -o root -g root -m 0755 "$helper_source" "$helper"
  install -d -o svxlink-node-updater -g svxlink-node-updater -m 0750 \
    /var/lib/svxlink-node-updater

  local sudoers_tmp
  sudoers_tmp="$(mktemp /etc/sudoers.d/svxlink-node-portal-agent.XXXXXX)"
  trap 'rm -f -- "$sudoers_tmp"' EXIT
  printf '%s\n' \
    'svxlink ALL=(root) NOPASSWD: /usr/local/libexec/svxlink-node-portal-config, /usr/local/libexec/svxlink-node-portal-update "", /usr/bin/systemctl start svxlink.service, /usr/bin/systemctl stop svxlink.service, /usr/bin/systemctl restart svxlink.service, /usr/bin/crontab -u svxlink -l, /usr/bin/crontab -u svxlink -, /usr/bin/crontab -u svxlink -r' \
    > "$sudoers_tmp"
  chmod 0440 "$sudoers_tmp"
  if ! visudo -cf "$sudoers_tmp"; then
    rm -f "$sudoers_tmp"
    say_error "Sudoers-regeln för uppdateringshjälpen är ogiltig."
    exit 1
  fi
  mv "$sudoers_tmp" "$sudoers"
  trap - EXIT
}

create_portal_environment() {
  if [[ -e "$PORTAL_ENV" ]]; then
    printf 'Behåller befintlig konfiguration: %s\n' "$PORTAL_ENV"
    chown root:root "$PORTAL_ENV"
    chmod 0600 "$PORTAL_ENV"
    return
  fi

  require_tty
  local username password confirmation
  while true; do
    prompt_value "Välj portalens användarnamn (1–64 tecken: A–Z, a–z, 0–9, punkt, _ eller -)"
    username="$REPLY"
    if [[ "$username" =~ ^[A-Za-z0-9._-]{1,64}$ ]]; then
      break
    fi
    say_error "Användarnamnet har ogiltigt format."
  done
  while true; do
    prompt_secret "Välj portalens lösenord (minst 16 tecken)"
    password="$REPLY"
    unset REPLY
    if (( ${#password} < 16 )); then
      say_error "Lösenordet måste innehålla minst 16 tecken."
      continue
    fi
    prompt_secret "Bekräfta lösenordet"
    confirmation="$REPLY"
    unset REPLY
    if [[ "$password" == "$confirmation" ]]; then
      break
    fi
    say_error "Lösenorden matchar inte."
  done

  write_environment "$PORTAL_ENV" \
    PORTAL_ADMIN_USER "$username" \
    PORTAL_ADMIN_PASSWORD "$password" \
    PORTAL_DATABASE "/var/lib/svxlink-node-portal/portal.sqlite3" \
    PORTAL_HOST "127.0.0.1" \
    PORTAL_PORT "8080"
  unset password confirmation REPLY
  printf 'Portalinställningar sparade i %s (root:root, 0600).\n' "$PORTAL_ENV"
}

create_agent_environment() {
  local portal_url node_id token config_path log_path interval
  if [[ -n "${AGENT_PORTAL_URL-}" && -n "${AGENT_NODE_ID-}" && -n "${AGENT_TOKEN-}" ]]; then
    # Uppgifterna kommer från installationskommandot i portalen: inga frågor och
    # befintlig konfiguration ersätts, så att noden kan anslutas till en ny portal.
    portal_url="$AGENT_PORTAL_URL"
    node_id="$AGENT_NODE_ID"
    token="$AGENT_TOKEN"
    config_path="${SVXLINK_CONFIG:-/etc/svxlink/svxlink.conf}"
    log_path="${SVXLINK_LOGFILE:-/var/log/svxlink}"
    interval="${AGENT_INTERVAL_SECONDS:-5}"
  else
    if [[ -e "$AGENT_ENV" ]]; then
      printf 'Behåller befintlig konfiguration: %s\n' "$AGENT_ENV"
      chown root:root "$AGENT_ENV"
      chmod 0600 "$AGENT_ENV"
      return
    fi

    require_tty
    prompt_value "Portalens HTTP- eller HTTPS-adress, exempel https://portal.example.net"
    portal_url="$REPLY"
    prompt_value "Nod-ID från provision-node"
    node_id="$REPLY"
    prompt_secret "Engångstoken från provision-node"
    token="$REPLY"
    unset REPLY
    prompt_value "SvxLinks konfigurationsfil" "/etc/svxlink/svxlink.conf"
    config_path="$REPLY"
    prompt_value "SvxLinks loggfil" "/var/log/svxlink"
    log_path="$REPLY"
    prompt_value "Rapporteringsintervall i sekunder (ändringar skickas direkt)" "5"
    interval="$REPLY"
  fi

  if ! python3 -c 'import sys; from urllib.parse import urlparse; u=urlparse(sys.argv[1]); sys.exit(0 if u.scheme in ("http", "https") and u.netloc and not u.username and not u.password and not u.query and not u.fragment else 1)' "$portal_url"; then
    say_error "Portaladressen måste vara en giltig HTTP- eller HTTPS-adress utan inloggningsuppgifter, query eller fragment."
    exit 1
  fi
  if (( ${#token} < 32 )); then
    say_error "Nodtoken måste innehålla minst 32 tecken."
    exit 1
  fi
  if [[ ! "$interval" =~ ^[0-9]+$ ]] || (( interval < 1 )); then
    say_error "Rapporteringsintervallet måste vara ett heltal på minst 1 sekund."
    exit 1
  fi
  if [[ -z "$node_id" || -z "$config_path" || -z "$log_path" ]]; then
    say_error "Nod-ID, konfigurationsfil och loggfil måste anges."
    exit 1
  fi

  write_environment "$AGENT_ENV" \
    AGENT_PORTAL_URL "$portal_url" \
    AGENT_NODE_ID "$node_id" \
    AGENT_TOKEN "$token" \
    AGENT_INTERVAL_SECONDS "$interval" \
    SVXLINK_CONFIG "$config_path" \
    SVXLINK_LOGFILE "$log_path"
  unset token REPLY AGENT_TOKEN
  printf 'Agentinställningar sparade i %s (root:root, 0600).\n' "$AGENT_ENV"
}

usage() {
  printf 'Användning: sudo bash %s [portal|agent|both]\n' "$(basename "$0")"
  printf 'Agenten installeras utan frågor om AGENT_PORTAL_URL, AGENT_NODE_ID och AGENT_TOKEN är satta.\n'
  printf 'Körs scriptet utan checkout (curl | bash) hämtas SVX_INSTALL_REPO och SVX_INSTALL_REF från GitHub.\n'
}

if [[ "${1-}" == "-h" || "${1-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ ${EUID} -ne 0 ]]; then
  say_error "Kör som root, till exempel: sudo bash ./install.sh portal"
  exit 1
fi

SCRIPT_SOURCE="${BASH_SOURCE[0]-}"
SCRIPT_DIR=""
if [[ -n "$SCRIPT_SOURCE" && -f "$SCRIPT_SOURCE" ]]; then
  SCRIPT_DIR="$(cd -- "$(dirname -- "$SCRIPT_SOURCE")" && pwd)"
fi

bootstrap_checkout() {
  local repo="${SVX_INSTALL_REPO:-https://github.com/formidablemagnus/svxlink-node-portal.git}"
  local ref="${SVX_INSTALL_REF:-main}"
  if [[ ! "$repo" =~ ^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
    say_error "SVX_INSTALL_REPO måste vara en https://github.com/ägare/repo-adress."
    exit 1
  fi
  if [[ ! "$ref" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ || "$ref" == *..* ]]; then
    say_error "SVX_INSTALL_REF innehåller ogiltiga tecken."
    exit 1
  fi
  if ! command -v git >/dev/null 2>&1; then
    say_error "git saknas. Installera git och kör kommandot igen."
    exit 1
  fi
  local workdir rc=0
  workdir="$(mktemp -d)"
  trap 'rm -rf -- "$workdir"' EXIT
  trap 'exit 1' HUP INT TERM
  printf 'Hämtar %s (%s)...\n' "$repo" "$ref"
  if ! git clone --quiet --depth 1 --branch "$ref" -- "$repo" "$workdir/svxlink-node-portal"; then
    rm -rf -- "$workdir"
    say_error "Kunde inte hämta $repo ($ref)."
    exit 1
  fi
  SVX_BOOTSTRAPPED=1 bash "$workdir/svxlink-node-portal/install.sh" "$@" </dev/null || rc=$?
  rm -rf -- "$workdir"
  exit "$rc"
}

if [[ -z "${SVX_BOOTSTRAPPED-}" && ( -z "$SCRIPT_DIR" || -n "${SVX_INSTALL_REPO-}" ) ]] \
   && [[ -z "$SCRIPT_DIR" || ! -f "$SCRIPT_DIR/portal/server.py" ]]; then
  bootstrap_checkout "$@"
fi
if [[ $# -gt 1 ]]; then
  usage >&2
  exit 2
fi
ROLE="${1-}"
if [[ -z "$ROLE" ]]; then
  require_tty
  printf 'Välj installation:\n  1) Central portal\n  2) Nodagent\n  3) Portal och agent på samma server\nVal [1-3]: ' >/dev/tty
  IFS= read -r ROLE </dev/tty
  case "$ROLE" in
    1) ROLE=portal ;;
    2) ROLE=agent ;;
    3) ROLE=both ;;
    *) say_error "Välj 1, 2 eller 3."; exit 2 ;;
  esac
fi
case "$ROLE" in
  portal|agent|both) ;;
  *) usage >&2; exit 2 ;;
esac

if ! command -v systemctl >/dev/null 2>&1 || [[ ! -d /run/systemd/system ]]; then
  say_error "En Linux-server med aktiv systemd krävs."
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  say_error "Python 3 saknas. Installera python3 och kör scriptet igen."
  exit 1
fi
if [[ ! -f "$SCRIPT_DIR/portal/server.py" || ! -f "$SCRIPT_DIR/agent/agent.py" || ! -f "$SCRIPT_DIR/config_helper.py" || ! -f "$SCRIPT_DIR/web/index.html" ]]; then
  say_error "Kör installationsscriptet från en komplett checkout av svxlink-node-portal."
  exit 1
fi
if [[ "$ROLE" == agent || "$ROLE" == both ]] && ! id svxlink >/dev/null 2>&1; then
  say_error "Användaren 'svxlink' saknas. Installera SvxLink först."
  exit 1
fi

install -d -o root -g root -m 0755 "$APP_DIR"
# Använd taggen om en tagg är utcheckad, annars filen VERSION.
release_version="$(git -C "$SCRIPT_DIR" describe --tags --exact-match 2>/dev/null || true)"
if [[ -n "$release_version" ]]; then
  printf '%s\n' "${release_version#v}" > "$APP_DIR/VERSION"
else
  cp "$SCRIPT_DIR/VERSION" "$APP_DIR/VERSION"
fi
chown root:root "$APP_DIR/VERSION"
chmod 0644 "$APP_DIR/VERSION"
case "$ROLE" in
  portal)
    if ! id svxlink-node-portal >/dev/null 2>&1; then
      useradd --system --user-group --home-dir /var/lib/svxlink-node-portal \
        --no-create-home --shell /usr/sbin/nologin svxlink-node-portal
    elif ! getent group svxlink-node-portal >/dev/null 2>&1; then
      say_error "Användaren svxlink-node-portal saknar en grupp med samma namn."
      exit 1
    fi
    install -d -o svxlink-node-portal -g svxlink-node-portal -m 0750 /var/lib/svxlink-node-portal
    install_component portal
    install_component web
    create_portal_environment
    install_service svxlink-node-portal.service
    ;;
  agent)
    install_component agent
    install_agent_config_helper
    install_agent_update_helpers
    create_agent_environment
    install_service svxlink-node-agent.service
    ;;
  both)
    if ! id svxlink-node-portal >/dev/null 2>&1; then
      useradd --system --user-group --home-dir /var/lib/svxlink-node-portal \
        --no-create-home --shell /usr/sbin/nologin svxlink-node-portal
    elif ! getent group svxlink-node-portal >/dev/null 2>&1; then
      say_error "Användaren svxlink-node-portal saknar en grupp med samma namn."
      exit 1
    fi
    install -d -o svxlink-node-portal -g svxlink-node-portal -m 0750 /var/lib/svxlink-node-portal
    install_component portal
    install_component web
    install_component agent
    install_agent_config_helper
    install_agent_update_helpers
    create_portal_environment
    create_agent_environment
    install_service svxlink-node-portal.service
    install_service svxlink-node-agent.service
    ;;
esac

systemctl daemon-reload
restart_service() {
  local service="$1"
  if ! systemctl restart "$service" || ! systemctl is-active --quiet "$service"; then
    systemctl status "$service" --no-pager || true
    journalctl -u "$service" -n 50 --no-pager || true
    exit 1
  fi
}
if [[ "$ROLE" == portal || "$ROLE" == both ]]; then
  systemctl enable svxlink-node-portal.service
  restart_service svxlink-node-portal.service
fi
if [[ "$ROLE" == agent || "$ROLE" == both ]]; then
  systemctl enable svxlink-node-agent.service
  restart_service svxlink-node-agent.service
fi

printf '\nInstallationen är klar. Kontrollera tjänstens status och logg med:\n'
if [[ "$ROLE" == portal || "$ROLE" == both ]]; then
  printf '  sudo systemctl status svxlink-node-portal.service\n'
  printf '  sudo journalctl -u svxlink-node-portal.service -n 50 --no-pager\n'
fi
if [[ "$ROLE" == agent || "$ROLE" == both ]]; then
  printf '  sudo systemctl status svxlink-node-agent.service\n'
  printf '  sudo journalctl -u svxlink-node-agent.service -n 50 --no-pager\n'
fi
