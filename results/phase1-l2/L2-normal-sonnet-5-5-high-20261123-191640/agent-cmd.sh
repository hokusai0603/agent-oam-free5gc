#!/usr/bin/env bash
# 由 run_trial.sh 產生，在 VM 內以 vagrant 使用者執行
cd ~/work
export PATH=$HOME/.local/bin:$PATH DISABLE_AUTOUPDATER=1
. ~/trial/auth.env
rm -f ~/trial/auth.env
date +%s > ~/trial/start
timeout 60m claude -p "$(cat ~/trial/prompt.md)" \
  --model claude-sonnet-5-5 --effort high \
  --output-format stream-json --verbose \
  --dangerously-skip-permissions \
  --disallowedTools WebSearch WebFetch \
  --max-budget-usd 15 \
  --no-session-persistence \
  > ~/trial/transcript.jsonl 2> ~/trial/stderr.log
echo $? > ~/trial/exit_code
date +%s > ~/trial/end
