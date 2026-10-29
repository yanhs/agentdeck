# envcompat.sh — the shell side of envcompat.py, sourced by start.sh and
# docker/entrypoint.sh (install.sh runs on its own, piped from curl, and spells the same
# rule inline: ${AGENTKENNEL_X:-${AGENTDECK_X:-default}}).
#
# AgentKennel's settings were AGENTDECK_* until v2.1.0, and existing .env files, compose
# overrides and `docker run -e` still set those. envcompat_sync:
#   1. fills each empty AGENTKENNEL_<X> from a set AGENTDECK_<X> (the new name wins);
#   2. exports every AGENTKENNEL_<X> under the old name too, for the programs started from
#      here (hooks and the owner's own tools written earlier read the old one).
# Run it again after setting defaults under the new names, so children get both.

envcompat_sync() {
  local v n
  for v in $(compgen -e); do
    case "$v" in
      AGENTDECK_*)   # compat: a setting under its name before v2.1.0
        n="AGENTKENNEL_${v#AGENTDECK_}"
        [ -n "${!n:-}" ] || export "$n=${!v}" ;;
    esac
  done
  for v in $(compgen -e); do
    case "$v" in
      AGENTKENNEL_*) export "AGENTDECK_${v#AGENTKENNEL_}=${!v}" ;;   # compat: both names
    esac
  done
}
