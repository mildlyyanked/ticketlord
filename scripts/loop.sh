#!/usr/bin/env bash
# Keep one GitHub Actions job alive for a few hours, checking every few minutes.
#
# GitHub only starts scheduled workflows every few hours, however often the cron asks, so a single
# check per run leaves long gaps. On a PUBLIC repo Actions minutes are free, so each run instead loops
# until shortly before the 6-hour job limit and the next scheduled run takes over. On a private repo
# (2,000 free minutes a month) that would use up the allowance in days, so it does one check and exits.
#
# Settings (repository variables): TICKETLORD_LOOP_INTERVAL minutes between checks (default 10),
# TICKETLORD_LOOP_MINUTES how long one run keeps going (default 340).
set -uo pipefail

interval=$(( ${TICKETLORD_LOOP_INTERVAL:-10} * 60 ))
duration=$(( ${TICKETLORD_LOOP_MINUTES:-340} * 60 ))

private=$(curl -fsS -H "Authorization: Bearer ${GITHUB_TOKEN}" \
  "https://api.github.com/repos/${GITHUB_REPOSITORY}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["private"])' 2>/dev/null || echo "True")
if [ "$private" != "False" ]; then
  echo "Repository is private (or visibility unknown): one check only, to stay within the free Actions minutes."
  duration=0
fi

seen_hash() {  # fingerprint of the screenings recorded so far (not the check-in counters)
  python3 -c 'import hashlib,json,sys; print(hashlib.sha1(json.dumps(json.load(open(sys.argv[1]))["seen"], sort_keys=True).encode()).hexdigest())' state/seen.json 2>/dev/null
}

save_state() {  # commit state/seen.json if it changed; $1 = reason
  git add state/seen.json 2>/dev/null || return 0
  git diff --cached --quiet && return 0
  git commit -q -m "Update seen screenings [skip ci]" -m "$1"
  for attempt in 1 2 3; do
    git pull -q --rebase && git push -q && return 0
    sleep $((attempt * 5))
  done
  echo "::warning::could not push state"
}

start=$(date +%s)
status=0
while :; do
  before=$(seen_hash)
  ticketlord check
  status=$?
  [ $status -ne 0 ] && echo "::warning::check exited with $status"
  # Push straight away when something new was recorded, so a later run can't alert twice;
  # the check-in counters alone can wait for the end of the run.
  [ "$(seen_hash)" != "$before" ] && save_state "new screenings recorded"
  elapsed=$(( $(date +%s) - start ))
  [ $(( elapsed + interval )) -ge $duration ] && break
  sleep "$interval"
done
save_state "end of run"
exit $status
