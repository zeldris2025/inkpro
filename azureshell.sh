#!/usr/bin/env bash
# Run a manage.py command on the live App Service container, over SSH.
#
# SSH drops you into a shell where neither the app's directory nor its
# virtualenv is active: the Oryx build extracts the app to /tmp/<id> with its
# packages in /tmp/<id>/antenv, while /home/site/wwwroot holds only the source.
# Running "python manage.py ..." from wwwroot therefore fails with
# ModuleNotFoundError: No module named 'django' — the code is there, the
# packages are not. This finds the right pair and uses them.
#
#     bash /home/site/wwwroot/azureshell.sh graphcheck
#     bash /home/site/wwwroot/azureshell.sh mediacheck --verbose
#     bash /home/site/wwwroot/azureshell.sh            # interactive shell
#
# Application settings are already present as environment variables in an SSH
# session, so commands see exactly the configuration the running site sees.
set -uo pipefail

APP_DIR="${APP_DIR:-/home/site/wwwroot}"

# The virtualenv Oryx built, wherever this image put it. Newest first, so a
# stale extraction from a previous deploy is not picked over the current one.
find_venv() {
  local candidate
  for candidate in $(ls -dt /tmp/*/antenv /home/site/wwwroot/antenv /antenv 2>/dev/null); do
    if [ -x "$candidate/bin/python" ]; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

if VENV="$(find_venv)"; then
  # shellcheck disable=SC1091
  source "$VENV/bin/activate"
  echo "==> virtualenv: $VENV"
else
  echo "!! No Oryx virtualenv found — falling back to the system python."
  echo "!! If commands fail with ModuleNotFoundError, the build never ran:"
  echo "!! check that SCM_DO_BUILD_DURING_DEPLOYMENT=true and redeploy."
fi

cd "$APP_DIR" || { echo "!! No app directory at $APP_DIR"; exit 1; }
echo "==> app: $APP_DIR"
echo "==> python: $(python --version 2>&1)"

if [ "$#" -eq 0 ]; then
  echo "==> Interactive shell. Try: python manage.py graphcheck"
  exec bash
fi

exec python manage.py "$@"
