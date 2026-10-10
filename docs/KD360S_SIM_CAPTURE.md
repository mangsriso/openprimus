# Capture simulated shots

Run the SDL simulator and save each shot through the real WPS1 UDP collector. The launcher creates a private run folder with SQLite, per-shot CSV and JSONL, and an HTML report with pressure and flow graphs.

## Start a capture

Install SDL2 and Python 3.12 or newer. `scripts/run_sim.sh` creates `.venv` and installs the pinned ESPHome dependencies if needed. It uses an existing `.venv` when available. Set `PYTHON` to a Python 3.12 executable if `python3.12` is not on your path.

From the repository root, run:

```sh
python3 scripts/kd360s_sim.py run
```

1. Wait for the `openprimus-sim` window. The first run compiles the simulator.
2. Select a brew mode, then click **Start**.
3. Click **Stop** to end the shot. **Back** also ends an active shot.
4. Open the `Report` URL printed in the terminal. The report refreshes every two seconds while the launcher runs.
5. Start more shots in the same window. Each shot gets separate exports and graphs.
6. Close the simulator window or press Ctrl-C in the launcher terminal to stop the capture.

The graph is in `report.html`. The SDL window displays a pressure gauge. The report reads archived SQLite samples, so a stopped shot can briefly show `pending` while the sender drains its queue.

Each run has a unique directory under `private/simulation/`. The directory contains:

- `snapshots.sqlite`, the unchanged collector's immutable WPS1 archive, including idle samples.
- `lifecycle.jsonl`, durable simulation session and shot boundaries.
- `shot-NNNN.csv` and `shot-NNNN.jsonl`, decoded archived records within each shot's sequence range.
- `shot-NNNN.svg`, pressure and flow plotted from those records.
- `summary.json` and `report.html`, completeness, missing sequence ranges, and drop counts.
- `simulator.log`, `collector.log`, and `launcher.json`, run diagnostics.

If the collector process exits, the launcher stops the simulator and reports an error. It does not restart the collector automatically. The native bridge tests exercise receiver recovery separately.

The launcher owns its simulator and collector processes. It stops only those processes on exit. Generated captures are ignored by Git. The ordinary `scripts/run_sim.sh` command still opens the simulator without capture unless the capture environment is supplied.

## Regenerate a report

Keep both `snapshots.sqlite` and `lifecycle.jsonl`. To regenerate derived exports and graphs, run:

```sh
python3 scripts/kd360s_sim.py report private/simulation/YOUR-RUN-DIRECTORY
```

`pressure` and `target_pressure` use bar. `flow` and `target_flow` use ml/s. Each exported record has `source: simulation`, the shot number, and elapsed seconds. WPS1 contains no temperature or weight. The simulator has no flow target, so `target_flow` is null in JSONL and blank in CSV. Raw volume and reserved fields remain zero.

The report uses these states:

| State | Meaning |
| --- | --- |
| `complete` | A durable stop boundary exists and every attempted sequence in the shot is archived. |
| `pending` | The shot ended but some samples have not reached SQLite yet. |
| `loss` | The ring dropped samples, or the launcher exited with missing shot samples. |
| `open_or_interrupted` | The stop boundary is missing or capture metadata has an error. |

A closed window does not prove that all queued records reached the collector. Keep the window open while a report shows `pending`. A crash during a shot leaves it open or interrupted. Missing sequences and nonfinite readings break plot lines.

## Run the automated scenario

The opt-in scenario executes the same `brew_toggle` and `brew_back` action scripts as the buttons. It performs a three-second shot, pauses, performs a four-second shot, returns home, and exits after two seconds of idle drain time.

```sh
python3 scripts/kd360s_sim.py run --scenario
python3 -m unittest discover -s tests -p 'test_kd360s_sim.py' -v
```

To reuse an already compiled SDL executable, pass `--program /absolute/path/to/program`. To retain a build for later runs, set `ESPHOME_BUILD_PATH` to an absolute directory without spaces before running `scripts/run_sim.sh`. Without that override, the script creates and removes its own temporary build directory.

The tests compile `firmware/host/sim_capture.h` with the canonical `experiments/kd360s_snapshot/protocol.h`. They exercise multiple shots, a collector process restart, backlog, wrong and lost ACKs, overflow at the end of a shot, immediate Start and Stop, Back, interrupted metadata, and exported plot gaps.

## Scope of the simulation

The host bridge samples every 100 ms and uses one session with increasing sequence numbers across shots and idle periods. Its 128-record ring drops new samples when full. ACKs must match the session, sequence, and loopback collector endpoint. UDP polling is nonblocking and bounded. The sender retries after 250 ms and sends the next record immediately after an ACK. The lifecycle journal flushes and fsyncs at shot boundaries, which can briefly pause the host UI. An `end_committed` marker is written only after the end record's fsync succeeds. Reports require that marker before claiming completeness.

This simulator runs the upstream ESPHome UI and simulated sensors. It does not run the stock Slim binary or the experimental firmware patch. Capture uses loopback UDP, not the WPM's Wi-Fi. A passing simulation verifies the host bridge, transport, archive, and report. Machine sampling, continuous Wi-Fi, firmware memory layout, and recovery still require a separate hardware test.
