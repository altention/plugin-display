#!/bin/bash
# Blind test: type questions into a channel Claude's tmux as if the owner wrote them (no hint to use display),
# one at a time, waiting until each turn ends; read the results with `altention dc read <channel>`.
# Don't send them with `altention dc post`: a question "from another Claude" makes it skip pictures.
# usage: tests/blind/run.sh <tmux session, e.g. dc-my-channel-1234> "question" ["question" ...]
# Start from a fresh conversation (`/clear` in that session) so it reads the current skill.
S=$1; shift
for q in "$@"; do
  tmux -L dcbot send-keys -t "$S" -l "$q"; sleep 1; tmux -L dcbot send-keys -t "$S" Enter; sleep 5
  until ! tmux -L dcbot capture-pane -p -t "$S" | tail -3 | grep -q "esc to interrupt"; do sleep 3; done
  sleep 10; echo "done: $q"
done
