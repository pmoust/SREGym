# The scored run: 21 sregym-lite problems x 3 attempts = 63 problem-runs.
#
# This shape is fixed deliberately. run-20260831-1752 was 21 x 3, so keeping it
# identical makes the next run a paired comparison against it and isolates what
# changed in between. Changing n_attempts or the problem set costs that
# comparison, which is most of the value.
#
# 63 paired problem-runs resolves about 23pp at 80% power. It is the smallest
# honest run, not a precise one: a 10 to 15pp claim needs 150 to 336, i.e. 7 to
# 16 attempts on Lite or the full 90-problem suite x 3.
#
# problems is left unset, which means the whole set. A narrowed run is not
# comparable with this one.

instance_count        = 3
runs_per_instance     = 3
n_attempts            = 3
agent_timeout_seconds = 1800
noise                 = false
