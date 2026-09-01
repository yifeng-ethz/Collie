# Changes in this fork

This fork exists because **upstream `bytedance/Collie` was archived (read-only) on
2025-07-21**, so the fixes below cannot be contributed as issues or pull requests. Every
item was found while running Collie to hunt for RNIC anomalies on a 100 GbE RoCEv2 fabric
where **PFC is disabled**, so the fabric is lossy.

The upstream README is explicit that Collie "does not take network (fabric) effect into
consideration", and the shipped `example.json` points both `iplist` entries at the same IP.
Much of what follows is therefore us running the tool outside the envelope it was validated
in, not a claim that it is broken as designed. It is recorded here because several items are
general (they bite on any fabric), and the lossy-fabric ones fail *silently*: the search keeps
running and scores affected points as clean.

Everything below was observed on the wire and re-verified against the source at `f16217c`;
**all file:line citations are against upstream `f16217c`, not against this fork.** Where we
could not establish a root cause we say so.

Credit where it is due: the tool did find a real anomaly for us (SGE-gather-induced
`packet_seq_err`).

## Environment the observations come from

| | |
|---|---|
| NIC | ConnectX-5 Ex, 100 GbE, `mlx5_0` |
| Firmware | 16.32.2004 (two nodes), 16.31.2006 (one node) |
| Transport | RoCEv2, GID index 3, port MTU 4096, ToS 105 |
| Fabric | 2-tier leaf-spine Clos with VXLAN overlay, **PFC off** (hardware PFCC register), lossy; global pause emitted by receivers is absorbed at the first hop; DCQCN active at firmware default (CNP loop works; the NIC stamps ECT(0) on every RoCEv2 packet regardless of the requested ToS, so Not-ECT RoCE cannot be produced from verbs) |
| OS | Ubuntu 22.04, kernel 6.8; in-box `mlx5_core` + rdma-core (no MLNX_OFED, no `mlnx_perf`) |
| Collie | upstream `f16217c`, engine built `make -j8` (no GDR) |
| Topology | search driven from a third host over SSH; endpoints are two cluster nodes |

Scale: ~300 engine-driven points over five phases (plus a 37-point annealing run), 2–3
repeats each.

## Status summary

| Item | What it is | Status in this fork |
|---|---|---|
| A1 | `--qp_timeout` defaults to 0 (infinite local ACK timeout) | fixed in `ad89bb5` |
| A2 | `check_run()` only sees the host it runs on | **not addressed** — see *Known limitations* |
| A3 | RTS deadline is ~10 s | fixed in `e0841fd` |
| A4 | The only backpressure signal is PFC pause duration | fixed in `4b95f4d` (new monitor class) |
| A5 | Every remote command is `ssh <user>@<data-plane IP>` | **not addressed** — see *Known limitations* |
| B1 | Only one endpoint is ever monitored | **not addressed** — see *Known limitations* |
| B2 | The annealing acceptance test is a no-op | fixed in `a5abc7a` — **changes search behaviour** |
| B3 | A failed setup does not tick the chain counter | fixed in `420a00f` |
| B4 | `rstrip` result discarded | fixed in `f0792ff` |
| B5 | `collie.py` discards the user's config | fixed in `368b4d1` |
| C1 | Client-only mode crashes on exit | fixed in `eaa1e98` |
| C2 | `--iters` counts loop iterations, not messages | documented, help text fixed in `228b77f` |
| C3 | `--send_batch` is WRs per `post_send`, not queue depth | documented in `4d3895c` |
| C4 | Dead flags (`--share_mr`, the sge-batch pair) | fixed in `c9f24c7` (flag no longer passed) |
| C5 | `--mtu` is inert for UD | fixed in `89abfd2` |
| C6 | The receiver never accumulates received bytes | fixed in `8321311` |
| D1–D7 | Harness robustness (numpy imports, ports, killall, MTU guard, degenerate bounds, wq/batch invariant, NUMA −1) | fixed in `9f91f1a` |

Fork-only additions, not upstream defects: `9965e83` (burst mode), `ac9ea64` (bounded engine
runs, configurable GID/ToS), `2834a85` (resource caps, log-uniform message sizes), `9c90154`
(configurable objective and schedule, per-run `summary.json`), `b8c6c53` (documented
`example.json`), `9117c7a` (`.gitignore`).

---

## A. Blocking on a lossy / PFC-off fabric

### A1. `--qp_timeout` defaults to 0 (infinite local ACK timeout) and the search never sets it

`traffic_engine/helper.cpp:20` defines `qp_timeout` with default `0`; `helper.cpp:198`
assigns it to `attr.timeout` in the RC RTS transition. Per the IB spec `timeout = 0` means the
local ACK timeout is infinite, so an RC requester that loses a packet waits forever instead of
retransmitting. `search/engine.py:60` and `:67` build the engine command lines and never pass
`--qp_timeout`, so every RC point inherits 0.

*Observed:* RC points reported `Rate=0` with no error and no CQE — the requester had stalled
permanently on the first drop. Invisible on same-host loopback, where nothing drops; with
`--qp_timeout=14` (~67 ms) the stalls disappeared.

**Status in this fork:** fixed in `ad89bb5`. The engine default is now 14 with a help text that
says what the value means, and `engine.py` passes `--qp_timeout` on both command lines from
`config["qp_timeout"]` (default 14). This is the single highest-value item for anyone running
Collie on a fabric that can drop.

### A2. `check_run()` only sees the host it runs on

`engine.py:33` runs `rdma res show qp | grep 'RTS.*collie_engine' | wc -l` via plain
`subprocess` — no `ssh` — and compares to `point.get_total_qps()` (`:153`). That is
self-consistent only when the searcher process *is* endpoint A: `Point.random()`
(`space.py:497`) anchors every traffic at host A, so A's local count does equal
`get_total_qps()`. Driving the search from a third machine (which `run_scripts()` otherwise
supports, since it `scp`s and `ssh`es to both IPs) makes the count 0, `set_up_traffic` returns
`-1`, and the point is silently skipped. `clean_process()` (`:189`) has the mirror blind spot:
it verifies the kill with a local `check_run(0)` and cannot see engines left running on the
peer.

*Fix would be:* run the check over `ssh` on each endpoint and sum, or document that
`collie.py` must run on `iplist[0]`.

**Status in this fork:** not addressed. Correcting it properly means giving the harness a
transport abstraction for "run this on endpoint X", which is the same change A5 and B1 need;
see *Known limitations*.

### A3. RTS deadline is ~10 s

`engine.py:34` loops `for i in range(10)` with `time.sleep(1)` at `:43`. Points with several
hundred QPs took past 60 s to reach RTS in our runs, and a premature `-1` is scored as a
failed setup, so large points are systematically under-sampled.

**Status in this fork:** fixed in `e0841fd`. `check_run(expected_n, retries=20)` — the wait is
now a parameter with a longer default, so a caller that knows its point is large can wait
longer. Our own driver waits up to 120 s.

### A4. The only backpressure signal is PFC pause duration

`bone.py:44-55` lists six metrics — vport RDMA bytes/packets plus `tx_prio3_pause_duration` /
`rx_prio3_pause_duration` — and `check_bone()` (`bone.py:79-81`) returns `-1` *only* when a
pause bar is exceeded. With PFC off, prio3 pause is identically zero, so the `-1` channel can
never fire and the only surviving verdict is the throughput bar `-2`. `_metrics` contains no
drop, discard, sequence-error or retransmit counter, so packet loss is invisible to the search
on any fabric. Separately, `mlnx_perf` (MFT/OFED) is root-only and simply absent on an
unprivileged HPC node.

*Observed:* our receiver emitted 802.3x **global** pause (`tx_pause_ctrl_phy`), not prio3 PFC
pause, and the switch did not relay it.

**Status in this fork:** fixed in `4b95f4d`, as an **additional monitor class** rather than a
change to `MlnxBoneMon`, whose behaviour is untouched. `bone.SysfsBoneMon` reads
`ethtool -S <netdev>` plus `/sys/class/infiniband/<dev>/ports/<n>/{hw_counters,counters}/`,
all unprivileged, takes a settle-then-window delta instead of a single sample, and makes the
`-1` verdict an explicit backpressure/loss test over `rx_out_of_buffer`, `out_of_buffer`,
`rx_discards_phy`, `packet_seq_err`, `out_of_sequence`, `roce_adp_retrans` and the pause
counters. `hardware.SysfsHwMon` exposes the diagnostic counters readable without root
(notably `outbound_pci_stalled_rd/wr`) from the window the bone monitor already measured.
Select it with `"monitor": "sysfs"` (the new default); `"monitor": "mlnx"` keeps the upstream
path. Relevant to upstream issue #1 (counter values not retrieved).

One caveat carried forward: **"any movement is an anomaly" does not work at 100 G
saturation** — 24 of our 28 clean ceiling points moved at least one of those counters. A
production threshold must be a per-second rate against a measured clean baseline, which is
what our external driver does; this fork ships the counters and the plumbing, not the
calibrated thresholds.

### A5. Every remote command is `ssh <username>@<data-plane IP>`

`engine.py:123`, `:129`, `:139`, `:167` and `space.py:84` all address endpoints by their RDMA
data-plane address with the single `username` from the JSON. Where those addresses are not SSH
endpoints, or sshd policy rejects node→node keys, the search hangs at `Space.__init__`
(`space.py:78-79` probes NUMA over SSH at construction) before the first point runs.

*Fix would be:* let `ip_to_host` map an IP to an arbitrary SSH target rather than just a
username, and allow a configured NUMA node instead of the probe.

**Status in this fork:** not addressed; see *Known limitations*. The NUMA half is mitigated —
`config["numa"]` overrides the probed range (`368b4d1`) and a `-1` result no longer reaches
`numactl` (D7) — but the SSH addressing itself is unchanged.

---

## B. Correctness of the search

### B1. Only one endpoint is ever monitored — receiver-side loss is invisible

`anneal.py:299` (and `:223`, `:252`, `:391`, `:467`) call `self._bonemon.monitor(self._bonedev_A)`.
`self._bonedev_B` is assigned at `anneal.py:91` and **never read anywhere in the file**;
`bone.py:59` runs `mlnx_perf` locally with no `ssh`, so passing B would not help without a
transport change.

*Observed, two cases:* (i) a single-QP UD stream where the receiver saturated at ~3.07 Mpps and
**silently discarded 47.5%** of the 5.85 Mpps offered load (`rx_discards_phy` 82.6 M over 30 s
on the receiver) while the sender-side view stayed pristine and the point scored clean; (ii) a
2→1 UD incast with delivery ratio exactly **0.500** and **zero** movement on every watched
counter on all three nodes — the drop was in the switch.

*Fix would be:* monitor both endpoints and merge, or at minimum sample the peer's rx counters
into `bone_results`.

**Status in this fork:** not addressed; see *Known limitations*. C6 (`8321311`) removes one
half of the obstacle by giving the engine a receiver-side byte count at all, which is what
makes per-sender delivery attribution in an N→1 UD test possible.

### B2. The annealing acceptance test is a no-op

`anneal.py:295` sets `prev_point = copy.deepcopy(point)` inside the MFS-dedup `while True`
loop, which runs *before* the acceptance test at `:325-334`. By the time the better/worse
comparison happens `prev_point` is already the newly mutated point, so both acceptance branches
(`:328`, `:334`) re-assign an identical value and the reject path never restores the previous
state. The chain is an unconditional random walk; temperature and `kAlpha` have no effect on
which point is kept.

**Status in this fork:** fixed in `a5abc7a` — the assignment is deleted and only the acceptance
test moves the chain anchor. **This commit changes search behaviour**, from a random walk to a
real Metropolis chain, and **upstream intent is unconfirmed**: we could not ask whether the
dedup loop was deliberately re-anchoring. It is isolated in its own commit so it can be
reverted without touching anything else.

### B3. A failed setup does not tick the chain counter

`anneal.py:297-298` — `if (self._engine.set_up_traffic(point)): continue` — skips
`anomaly_flag -= 1` at `:308`. Combined with B2 (`prev_point` already points at the broken
point), a structurally invalid point is mutated indefinitely without the chain ever reaching
`anomaly_flag <= 0` and restarting from a fresh random point.

**Status in this fork:** fixed in `420a00f` — decrement before the `continue`.

### B4. `rstrip` result discarded

`space.py:386` is `req_str.rstrip(',')` with the return value dropped, and `:387` returns the
unstripped string, so every `--request=` / `--receive=` carries a trailing comma. The engine's
splitter tolerates it, so this is cosmetic today, but it is one character from being a real
bug.

**Status in this fork:** fixed in `f0792ff` — `return req_str.rstrip(',')`.

### B5. `collie.py` discards the user's config

`collie.py:33` does `config = {}` and rebuilds the dict from `bars` plus an empty `counters`
list, so `example.json`'s own `diag_counters` key (`example.json:13`) is silently ignored.
`Director.__init__` is then called without `ibdev_A/B`, `bonedev_A/B` or
`A_numarange/B_numarange`, so those keep their hardcoded defaults (`mlx5_0`, `rdma0`) and
cannot be set from JSON at all — related to upstream issue #2.

**Status in this fork:** fixed in `368b4d1`. The parsed config is handed straight to the
`Director`, which now reads device names, monitor selection, measurement-window shape,
resource caps, objective and schedule out of it. Every key defaults to the previous hardcoded
value except `monitor`, which defaults to the new `sysfs` path. `example.json` (`b8c6c53`)
documents each key inline with a sibling `"_<key>"` line.

---

## C. Traffic engine

### C1. Client-only mode crashes on exit

`traffic_engine/main.cpp:49-50` unconditionally calls `listen_thread.join()` and
`server_thread.join()`. In client-only mode (`--connect=` without `--server`) both are
default-constructed and non-joinable, so `join()` throws
`std::system_error: Invalid argument` (EINVAL) and the process aborts. *Observed* on every
client-only invocation.

**Status in this fork:** fixed in `eaa1e98` — both joins are guarded with `joinable()`.

### C2. `--iters` counts loop iterations, not messages

`context.cpp:936` (`int iterations_left = FLAGS_iters;`) and `:940` (`iterations_left--`) sit
in the outer `while (true)`, and each pass posts `batch_size` WRs to *every* activated endpoint
(`:954`), where `req_vec` may itself hold several requests — so messages ≈
`iters × endpoints × batch × |req_vec|`, while the help text ("Iterations one QP will send",
`helper.cpp:53`) reads as per-QP messages. A pass in which a QP has no send credits still
consumes an iteration. Not load-bearing for the search, which always passes `--run_infinitely`
(`engine.py:67`), but it silently invalidates hand-driven runs.

**Status in this fork:** documented — the help text is corrected in `228b77f`. The alternative
fix (decrement per posted WR) would change the meaning of an existing flag, so it is left to
upstream.

### C3. `--send_batch` is WRs per `post_send`, not queue depth

Clear in the source (`helper.cpp:58`, `context.cpp:934`/`:954`) and the help text is accurate,
but it is easy to read `--send_batch=1` as "depth 1"; pipeline depth is `--send_wq_depth`,
default **1024** (`helper.cpp:42`). We lost a measurement phase to this: a "depth-1" arm built
with `--send_batch=1` was in fact pipelined 1024-deep and missed its model by 5×, while
`--send_wq_depth=1` reproduced the intended behaviour to within 1.3%.

**Status in this fork:** documented in `4d3895c` (`traffic_engine/README.md`). No code change.

### C4. Dead flags

`--share_mr` is defined at `helper.cpp:38` (declared `helper.hpp:55`) and **never read**
anywhere in the engine — yet `engine.py:60` and `:67` pass it on every command line, which
reads as if MR sharing were being exercised. `--send_sge_batch_size` and
`--recv_sge_batch_size` (`helper.cpp:55-56`) are likewise defined and never read; the
same-named search bounds at `space.py:73-74` *are* live, but only because they shape the
`w_<nsge>_<size>...` request string.

**Status in this fork:** fixed in `c9f24c7` — `--share_mr` is dropped from both `engine.py`
templates, and there is now a comment at the `space.py` sge-batch bounds explaining why they
are live so the next reader does not "fix" them by passing the flags. The three engine flags
themselves are left defined: deleting or implementing them is an upstream design call.

### C5. `--mtu` is inert for UD

In the RTR transition `helper.cpp:188-189` is `case IBV_QPT_UD: break;` — `attr.path_mtu` is
set and `IBV_QP_PATH_MTU` added to the mask only for RC/UC (`helper.cpp:172`, `:186`). This is
correct per the spec (UD uses the port MTU), but the search does not know it:
`MinimalFeatureSet` still sweeps `--mtu` for UD traffics, spending three runs per UD anomaly on
a parameter that cannot change anything.

**Status in this fork:** fixed in `89abfd2` — `generate_mfs_from_traffic` skips the MTU sweep
when the traffic's QP type is UD.

### C6. The receiver never accumulates received bytes

`endpoint.cpp:218-224` — `RecvHandler()` bumps `recv_credits_` and returns (the commented-out
body suggests this was intentional). `bytes_sent_now_` is incremented only in `PostSend`
(`endpoint.cpp:26`), so `PrintThroughput` (`:227`) on a receiver-side process reports nothing —
and because its first-call gate is `if (bytes_sent_last_ == 0)` it returns from that branch on
every call and never even starts an interval. For RC this is recoverable from the sender's
completions, but **UD has no sender-side delivery signal at all**, so per-sender delivery
attribution in an N→1 UD test is impossible with the engine as shipped.

**Status in this fork:** fixed in `8321311`. `RecvHandler` accumulates `wc->byte_len` and a
message count, and `PrintThroughput` starts its interval on either direction and reports the
receive side. Two details found while fixing it: `ParseEachEx()` (the `--hw_ts` extended-CQ
path, `context.cpp:787`) calls `RecvHandler(nullptr)`, so the byte count must be null-guarded
and is only available on the ordinary `PollEach()` path; and for UD `byte_len` includes the
40-byte GRH. A sender's output is byte-identical — the new log line is emitted only when the
endpoint has actually received something.

---

## D. Harness robustness (general, not lossy-fabric specific)

All seven are fixed in `9f91f1a`.

**D1. `search/logger.py:10` breaks on numpy ≥ 1.25.**
`from numpy.lib.stride_tricks import _maybe_view_as_subclass` — a private symbol that no longer
exists, and is unused in the file. On numpy 2.2.6 this is a hard `ImportError` that aborts the
search at import time. `anneal.py:16` (`from numpy.core.arrayprint import
format_float_scientific`) is also unused; it currently only emits a `DeprecationWarning`, since
`numpy.core` is still a shim, so it is not yet fatal. *Fix:* delete both lines. Highest-value
item here — the tool does not start on a current Python environment without it.

**D2. Port counter resets to 3000 for every point.** `engine.py:159-160`
(`clean(self, global_port=3000)`) resets `_global_port`, and `translate()` calls `self.clean()`
at `:47` on every point. Consecutive points reuse the same low out-of-band TCP ports while the
previous point's listeners are still in `TIME_WAIT`, and the bind fails. *Fix:* the counter
walks 20000–60000 monotonically and `clean()` clears only `_commands`; `log_scripts()` replays
a point on its own ports via a new `translate(base_port=)`, so the reproduce script still
matches the run that was measured.

**D3. `killall` failures are invisible.** `engine.py:167-168` runs
`ssh ... killall <absolute binary path>`. In our environment leftover engines survived between
points; we did not root-cause why `killall` with an absolute path failed here, and
`pkill -f '[c]ollie_engine'` reaps them reliably. Independently, the failure cannot be
detected: `subprocess.run` at `:170` has no `check=True`, so a nonzero exit is never raised and
`killall()` returns 0 regardless — only the (single-host, see A2) `check_run(0)` at `:189`
could catch it. *Fix:* `pkill -f` with a bracketed pattern, TERM then KILL. `clean.sh` also no
longer lands in the caller's cwd.

**D4. The MTU-shrink guard never fires (case mismatch).** `space.py:531` is
`if "MTU" in dim and delta_value < 0:`, but `dim` here is the attribute name `"_mtu"` — built
at `anneal.py:193` from `init_mutate_space`'s `traffic_list` (`anneal.py:182`), and used one
line earlier at `space.py:522` as `self._space._bounds[dim[1:]]` with key `"mtu"`.
`"MTU" in "_mtu"` is always `False`, so a shrinking MTU never regenerates the request vector,
leaving UD requests larger than the new path MTU — which the engine then rejects at setup.
*Fix:* `if "mtu" in dim`. One character.

**D5. Infinite loop when a search dimension has a degenerate bound.** `space.py:525-527`:

```python
        while delta_value == 0:
            delta_value = random.randint(bound[0] - cur_val, bound[1] - cur_val)
```

If `bound[0] == bound[1] == cur_val` — an ordinary configuration, e.g. pinning NUMA node 0 on
a single-socket machine, or disabling the GPU dimension — `randint(0, 0)` returns 0 forever and
the search hangs with no output. *Fix:* return early when `bound[0] == bound[1]`. This fork
also drops degenerate dimensions from the mutate space (`9c90154`).

**D6. `Endhost.random()` does not enforce `wq_depth >= batch`.** `space.py:133-136` draws
`send_wq_depth`, `recv_wq_depth`, `send_batch` and `recv_batch` independently, so a randomly
drawn point can ask the engine to post a 64-WR batch into a 1-deep work queue and the run dies
at setup. `Point.mutate()` already enforces the invariant at `space.py:538-541`; only the
random draw is missing it. *Fix:* the same two `max()` lines.

**D7. `update_numa` can return `-1` and it reaches `numactl`.** `space.py:83-89` returns the
raw contents of `/sys/class/infiniband/<dev>/device/numa_node`, which is **`-1`** when the
device reports no NUMA affinity. `_best_numa_node` is then read by `MinimalFeatureSet` at
`anneal.py:548-549` and assigned to `_numa_node`, which `engine.py:60` and `:67` format into
`numactl -N -1 -m -1 ...` — and every engine launch for that point fails.
*Fix:* `return max(best_numa_node, 0)`.

---

## Known limitations

**A2, A5 and B1 are not addressed in this fork, deliberately.** All three are the same
architectural gap: the harness has no notion of "run this command on endpoint X" that is
separate from "the RDMA address of endpoint X", and no way to merge observations from more
than one endpoint. Fixing them properly means a transport abstraction and a two-sided
measurement path — a redesign, not a patch, and one we did not want to land unilaterally in a
fork of an archived project.

They are addressed instead outside this repository, in the **`hacc-fpga-llm`** repo under
`tools/collie-soak/`: a third-host-driven runner that

* addresses each endpoint by a management SSH alias, independent of its data-plane IP (A5);
* counts RTS QPs on **both** endpoints over SSH and sums them, and verifies teardown the same
  way (A2);
* samples the loss/backpressure counters on **both** endpoints plus the peer's delivered
  bytes, so receiver-side and in-switch loss are visible (B1);
* scores counter movement as a per-second rate against a measured clean-ceiling baseline
  rather than against zero, and keeps the throughput-only definition of a "find" with
  loss signals as advisory tags (the A4 caveat above).

That runner imports `search/space.py` from this tree unmodified, so the point generator and
the fixes here are shared with it.

## Fork-only feature: burst mode

`--burst_size`, `--burst_gap_us` and `--burst_count` add a burst-paced client datapath and a
`BURSTSTATS` summary line, for replaying the alternate-then-idle pattern of a collective phase
rather than a saturating stream. It is **off by default** (`--burst_size=0`) and the default
path is byte-identical to upstream. See `traffic_engine/README.md`.

## Licence

Upstream `LICENSE` (MIT, ByteDance Inc. and Duke University) is unchanged. The changes in this
fork are released under the same MIT licence.
