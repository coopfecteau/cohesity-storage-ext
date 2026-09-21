#!/usr/bin/env bash
# Run terraform in e2e/terraform with the installer token taken from YOUR dotenv file.
#
#   e2e/tf.sh --env-file <path> [--token-var NAME] [--url-var NAME] -- <terraform args...>
#   e2e/tf.sh --env-file ~/secrets/dt.env -- apply
#
# Exports TF_VAR_dt_paas_token (from DT_OPERATOR_TOKEN by default) and TF_VAR_dt_environment_url
# (from DT_API_URL, else derived from DT_APPS_HOST), then execs terraform. The token is never
# echoed, never written to a tfvars file, and this script never runs under `set -x`.
# A variable already exported in your shell wins over the file.
#
# The file is parsed, not sourced: nothing in it is executed.
set -euo pipefail
set +x

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
env_file=""
token_var="DT_OPERATOR_TOKEN"
url_var="DT_API_URL"
apps_var="DT_APPS_HOST"

while [ $# -gt 0 ]; do
  case "$1" in
    --env-file) env_file="$2"; shift 2 ;;
    --token-var) token_var="$2"; shift 2 ;;
    --url-var) url_var="$2"; shift 2 ;;
    --apps-var) apps_var="$2"; shift 2 ;;
    --) shift; break ;;
    *) break ;;
  esac
done
[ $# -gt 0 ] || { echo "usage: $0 --env-file PATH -- <terraform args>" >&2; exit 2; }

# Print one KEY's value from the dotenv file: KEY=VALUE, optional `export `, optional quotes.
read_var() {
  local wanted="$1" line key value
  [ -n "$env_file" ] || return 0
  [ -r "$env_file" ] || { echo "cannot read env file $env_file" >&2; exit 2; }
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    line="${line#"${line%%[![:space:]]*}"}"
    case "$line" in ''|'#'*) continue ;; esac
    line="${line#export }"
    key="${line%%=*}"
    [ "$key" != "$line" ] || continue
    key="${key%"${key##*[![:space:]]}"}"
    [ "$key" = "$wanted" ] || continue
    value="${line#*=}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    case "$value" in
      \"*\") value="${value:1:${#value}-2}" ;;
      \'*\') value="${value:1:${#value}-2}" ;;
    esac
    printf '%s' "$value"
    return 0
  done < "$env_file"
}

if [ -z "${TF_VAR_dt_paas_token:-}" ]; then
  token="${!token_var:-}"
  [ -n "$token" ] || token="$(read_var "$token_var")"
  [ -n "$token" ] || { echo "$token_var is not set in the environment or in ${env_file:-<no --env-file>}" >&2; exit 2; }
  export TF_VAR_dt_paas_token="$token"
  unset token
fi

if [ -z "${TF_VAR_dt_environment_url:-}" ]; then
  url="${!url_var:-}"
  [ -n "$url" ] || url="$(read_var "$url_var")"
  if [ -z "$url" ]; then
    apps="${!apps_var:-}"
    [ -n "$apps" ] || apps="$(read_var "$apps_var")"
    case "$apps" in
      "") ;;
      *.apps.dynatrace.com*) url="${apps/.apps.dynatrace.com/.live.dynatrace.com}" ;;
      *.apps.*) url="${apps/.apps./.}" ;;
      *) url="$apps" ;;
    esac
  fi
  url="${url%/}"; url="${url%/api}"
  case "$url" in ""|https://*) ;; *) url="https://$url" ;; esac
  [ -n "$url" ] || { echo "neither $url_var nor $apps_var is set in the environment or ${env_file:-<no --env-file>}" >&2; exit 2; }
  export TF_VAR_dt_environment_url="$url"
  echo "tenant (classic host): $url"
fi

exec terraform -chdir="$here/terraform" "$@"
