# About this fork

Upstream `bytedance/Collie` was archived (read-only) on 2025-07-21, so these fixes cannot be filed as issues or PRs. They come from running Collie on a **lossy, PFC-off** 100 GbE RoCEv2 fabric with two real endpoints; upstream is validated on a same-host, lossless, PFC-enabled setup, and outside that envelope several failures are *silent* — the search keeps running and scores affected points as clean. Item-by-item changelog, with upstream `f16217c` file:line citations: [`CHANGES-hacc-fork.md`](CHANGES-hacc-fork.md).

Running on a lossy / PFC-off fabric, four things matter:

1. **`qp_timeout`** — upstream defaults it to 0 = *infinite* local ACK timeout, so the first dropped packet stalls an RC requester forever and the point measures 0 Gb/s with no error. Now 14 (~67 ms) by default and passed by the search; override with `"qp_timeout"` in the config.
2. **`"monitor": "sysfs"`** (the new default) reads `ethtool -S` plus InfiniBand sysfs — no `mlnx_perf`, no root — and makes the `-1` verdict a real backpressure/loss test. With PFC off, upstream's pause-duration signal is identically zero.
3. **Monitor both endpoints.** This fork still samples only endpoint A, so receiver-side and in-switch loss remain invisible to it (see *Known limitations* in the changelog).
4. **Define a "find" by throughput.** At 100 G saturation nearly every clean point moves some loss counter, so "any movement is an anomaly" is unusable; treat loss signals as advisory against a measured clean-ceiling baseline.

A third-host-driven runner that does 2–4 properly — dual-endpoint monitoring, management-alias SSH, rate-vs-baseline scoring — lives in the **`hacc-fpga-llm`** repo under `tools/collie-soak/`, and imports `search/space.py` from this tree unmodified.

New engine flags `--burst_size` / `--burst_gap_us` / `--burst_count` replay a collective's burst-then-idle pattern and print a `BURSTSTATS` line; off by default, see `traffic_engine/README.md`.

Fork changes are MIT, same as upstream.

---

# Collie
Collie is for uncovering RDMA NIC performance anomalies. 

# Overview

* [Prerequisite](#Prerequisite) 
* [Quick Start](#Quick-start)
* [Content](#Content)
* [Publication](#Publications)
* [Copyright](#Copyright)

# Prerequisite
- Two hosts with RDMA NICs.
  - Connected to the same switch is recommended since Collie currently does not take network(fabric) effect into consideration. But Collie should work once two hosts are connected and RDMA communication enabled.   

- Set up passwordless SSH login (e.g., ssh public/private keys login).
  - Collie currently uses passwordless SSH login to run traffic_engine on different hosts.

- Google gflags and glog library installed. 
  - Collie uses glog for logging and gflags for commandline flags processing.

- Collie should supports all types of RDMA NICs and drivers that follow IB verbs specification, but currently we've only tested with Mellanox and Broadcom RNICs. 

# Quick Start

## Environment Setup
- Install prerequisites.

```
apt-get install -y libgflags-dev libgoogle-glog-dev
```

- Setup passwordless SSH login.

## Build Traffic Engine

- Build the traffic engine without GPU and CUDA:

``` 
cd traffic_engine && make -j8
```

- OR buidl the traffic engine that supports GPU Direct RDMA:

``` 
cd traffic_engine && GDR=1 make -j8
``` 

NOTICE: GDR is supported only for Tesla or Quadro GPUs according to [GPUDirect RDMA](https://docs.nvidia.com/cuda/gpudirect-rdma/index.html).

Please refer to `traffic_engine/README` for more details.

## How to Run: Arguments and Examples

Collie uses JSON configuration file to set parameters for a given RDMA subsystem. 
- Configuration Example: see `./example.json`
  - **username** -- Collie uses SSH to run engines on different hosts, so it needs the username for login.
  - **iplist** --  the client IP and the server IP, given in a list.
  - **logpath** --  the logging path for Collie. Users can get detailed results of anomalies and the reproduce scripts for Collie here.
  - **engine** -- the path for traffic engine.
  - **iters** -- at most `iters` tests that Collie would run.
  - **bars** --  user's expected performance. 
    - tx_pfc_bar -- TX (sent) PFC pause duration in us per second. 
    - rx_pfc_bar -- RX (received) PFC pause duration in us per second.
    - bps_bar -- bits per second of the entire NIC.
    - pps_bar -- packets per second of the entire NIC.
  
- Quick Run Example

``` 
python3 search/collie.py --config  ./example.json
```



# Content
Collie consists of two components, the traffic engine and the search algorithms (the monitor is included as a part of search algorithm).
- Traffic Engine (`./traffic_engine`)
  
  Traffic engine is an independent part that implemented in C/C++. Users can use the engine to generate flexible traffic of different patterns. See `./traffic_engine/README` for more details and examples of complex traffic patterns.It is recommended to reproduce the anomalies (see Appendix of our NSDI paper) with the tool. 

- Search Algorithms (`./search`)
  
  Our simulated-annealing (SA) based algorithm and minimal feature set (MFS) are implemented in python scripts. 
  - `space.py` -- the search space. `Space` defines the search space (upper/lower bounds, granularity for each parameter). Each `Point` has several `Traffics` (e.g., one A->B and one B->A). Each `Traffic` has two `Endhost`, one server and one client, as well as many other attributes that describe this traffic (e.g., QP type).
  - `engine.py` -- given a point, running `collie_engine ` to set up the corresponding traffic described in the `Point`. If users need to set up traffics in different ways (rather than SSH), please modify the `Engine` class.
  - `anneal.py` -- the simulated-annealing based algorithm and minimal feature set algorithm are implemented here. If users need to modify the temperature and mutation logics, please modify here.
  - `logger.py` -- logging assistant functions for logging results and reproduce scripts. 
  - `bone.py` -- monitor performance counters and collect statistic results based on vendor's tools.
  - `hardware.py` -- monitor diagnostic counters and collect statistic results based on vendor's tools.  (Unfortunately currently diagnostic counters tools like [NeoHost](https://support.mellanox.com/s/productdetails/a2v50000000N2OlAAK/mellanox-neohost) is not publicly available and open-sourced, so we only provide performance counter based code for NDA reasons.)
  - `collie.py` -- read user parameters and call SA to search. 
  

# Copyright

Collie is provided under the MIT license. See LICENSE for more details.
