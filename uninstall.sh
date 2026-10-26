#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="/opt/svxlink-node-portal"
APP_DIR="$APP_ROOT/app"
PORTAL_ENV="/etc/default/svxlink-node-portal"
AGENT_ENV="/etc/default/svxlink-node-agent"
SYSTEMD_DIR="/etc/systemd/system"
DATA_DIR="/var/lib/svxlink-node-portal"
HELPER="/usr/local/libexec/svxlink-node-portal-config"
SUDOERS="/etc/sudoers.d/svxlink-node-portal-agent"
PORTAL_USER="svxlink-node-portal"

say_error() {
  printf 'Fel: %s\n' "$*" >&2
}

usage() {
  printf 'Användning: sudo bash %s [portal|agent|both] [--purge-data] [--yes]\n' "$(basename "$0")"
  printf '\n'
  printf '  portal        Ta bort den centrala portalen\n'
  printf '  agent         Ta bort nodagenten\n'
  printf '  both          Ta bort båda\n'
  printf '  --purge-data  Radera även portalens databas och systemanvändare (kan inte ångras).\n'
  printf '                Ladda ner en säkerhetskopia från Admin-sidan först.\n'
  printf '  --yes         Fråga inte efter bekräftelse\n'
  printf '\nUtan --purge-data behålls databasen och inställningsfilerna så att en ominstallation\n'
  printf 'kan fortsätta där den slutade.\n'
}

ROLE=""
PURGE=0
ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    -h|--help) usage; exit 0 ;;
    --purge-data) PURGE=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    portal|agent|both)
      if [[ -n "$ROLE" ]]; then usage >&2; exit 2; fi
      ROLE="$arg"
      ;;
    *) usage >&2; exit 2 ;;
  esac
done

if [[ ${EUID} -ne 0 ]]; then
  say_error "Kör som root, till exempel: sudo bash ./uninstall.sh portal"
  exit 1
fi

if [[ -z "$ROLE" ]]; then
  if [[ ! -r /dev/tty ]]; then
    say_error "Ange portal, agent eller both."
    exit 2
  fi
  printf 'Välj vad som ska tas bort:\n  1) Central portal\n  2) Nodagent\n  3) Båda\nVal [1-3]: ' >/dev/tty
  IFS= read -r choice </dev/tty
  case "$choice" in
    1) ROLE=portal ;;
    2) ROLE=agent ;;
    3) ROLE=both ;;
    *) say_error "Välj 1, 2 eller 3."; exit 2 ;;
  esac
fi

REMOVE_PORTAL=0
REMOVE_AGENT=0
[[ "$ROLE" == portal || "$ROLE" == both ]] && REMOVE_PORTAL=1
[[ "$ROLE" == agent || "$ROLE" == both ]] && REMOVE_AGENT=1

if (( ! ASSUME_YES )); then
  if [[ ! -r /dev/tty ]]; then
    say_error "Ingen terminal tillgänglig. Använd --yes för att köra utan bekräftelse."
    exit 1
  fi
  printf 'Detta tar bort: %s%s\n' \
    "$( (( REMOVE_PORTAL )) && printf 'portal ' )" \
    "$( (( REMOVE_AGENT )) && printf 'agent' )" >/dev/tty
  if (( REMOVE_PORTAL && PURGE )); then
    printf 'VARNING: Portalens databas (%s) raderas permanent.\n' "$DATA_DIR" >/dev/tty
  fi
  printf 'Fortsätta? [j/N]: ' >/dev/tty
  IFS= read -r answer </dev/tty
  case "$answer" in
    j|J|y|Y|ja|Ja|yes) ;;
    *) printf 'Avbrutet.\n'; exit 0 ;;
  esac
fi

HAVE_SYSTEMCTL=0
if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
  HAVE_SYSTEMCTL=1
fi

remove_service() {
  local name="$1"
  if (( HAVE_SYSTEMCTL )); then
    systemctl disable --now "$name" >/dev/null 2>&1 || true
  fi
  rm -f "$SYSTEMD_DIR/$name"
}

if (( REMOVE_PORTAL )); then
  remove_service svxlink-node-portal.service
  rm -rf -- "$APP_DIR/portal" "$APP_DIR/web"
  printf 'Portalen är borttagen.\n'
  if (( PURGE )); then
    rm -rf -- "$DATA_DIR"
    rm -f "$PORTAL_ENV"
    if id "$PORTAL_USER" >/dev/null 2>&1; then
      userdel "$PORTAL_USER" >/dev/null 2>&1 || true
    fi
    if getent group "$PORTAL_USER" >/dev/null 2>&1; then
      groupdel "$PORTAL_USER" >/dev/null 2>&1 || true
    fi
    printf 'Portalens data, inställningar och systemanvändare är raderade.\n'
  else
    printf 'Databasen (%s) och %s behölls. Använd --purge-data för att radera dem.\n' \
      "$DATA_DIR" "$PORTAL_ENV"
  fi
fi

if (( REMOVE_AGENT )); then
  remove_service svxlink-node-agent.service
  rm -rf -- "$APP_DIR/agent"
  rm -f "$HELPER" "$SUDOERS" "$AGENT_ENV"
  rmdir /usr/local/libexec 2>/dev/null || true
  printf 'Nodagenten är borttagen (inställningar och sudoers-regel raderade).\n'
  printf 'SvxLink och användaren svxlink påverkas inte.\n'
fi

if (( HAVE_SYSTEMCTL )); then
  systemctl daemon-reload
  systemctl reset-failed >/dev/null 2>&1 || true
fi

# Ta bort den delade programkatalogen först när ingen komponent finns kvar.
if [[ ! -d "$APP_DIR/portal" && ! -d "$APP_DIR/web" && ! -d "$APP_DIR/agent" ]]; then
  rm -rf -- "$APP_ROOT"
fi

printf 'Avinstallationen är klar.\n'
