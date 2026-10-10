#!/usr/bin/env python3
"""Capture simulated shots through the production WPS1 receiver and plot archived rows."""
from __future__ import annotations

import argparse
from contextlib import closing
import csv
import html
import io
import json
import os
from pathlib import Path
import secrets
import selectors
import signal
import subprocess
import sys
import tempfile
import time

import kd360s_live as live

ROOT = Path(__file__).resolve().parents[1]


def atomic_text(path, text):
    with tempfile.NamedTemporaryFile(mode="w", prefix=f".{path.name}.", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(text)
            output.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def read_events(path):
    if not path.exists():
        return [], []
    events, errors = [], []
    data = path.read_text()
    for index, line in enumerate(data.splitlines(keepends=True), 1):
        if not line.endswith("\n"):
            errors.append("Incomplete lifecycle journal tail")
            break
        try:
            events.append(json.loads(line))
        except ValueError:
            errors.append(f"Invalid lifecycle journal line {index}")
            break
    return events, errors


def missing_ranges(first, end, rows):
    cursor, ranges = first, []
    for row in rows:
        seq = row["seq"]
        if cursor < seq:
            ranges.append([cursor, seq])
        cursor = seq + 1
    if cursor < end:
        ranges.append([cursor, end])
    return ranges


def segments(rows, field):
    result, current, previous = [], [], None
    for row in rows:
        value = row[field]
        if value is None or (previous is not None and row["seq"] != previous + 1):
            if current:
                result.append(current)
            current = []
        if value is not None:
            current.append((row["elapsed_s"], value))
        previous = row["seq"]
    if current:
        result.append(current)
    return result


def plot(rows, duration):
    groups = [("pressure", "Pressure (bar)", "#60a5fa"),
              ("target_pressure", "Target pressure (bar)", "#f59e0b"),
              ("flow", "Flow (ml/s)", "#34d399"),
              ("target_flow", "Target flow (ml/s)", "#f472b6")]
    svg = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 480" role="img" aria-label="Archived simulated pressure and flow">',
           '<rect width="800" height="480" fill="#111827"/>',
           '<text x="55" y="20" fill="white">SIMULATION · archived WPS1 samples</text>']
    for panel, title in enumerate(("Pressure (bar)", "Flow (ml/s)")):
        top = 60 + panel * 190
        fields = groups[panel * 2:panel * 2 + 2]
        maximum = max([12.0] + [r[f] for f, _, _ in fields for r in rows if r[f] is not None])
        svg.append(f'<text x="55" y="{top - 12}" fill="white">{title}, scale 0–{maximum:.1f}</text>')
        svg.append(f'<path d="M55 {top} V{top + 140} H770" stroke="#94a3b8" fill="none"/>')
        for field, _, color in fields:
            for section in segments(rows, field):
                points = " ".join(f"{55 + t / max(duration, .001) * 710:.2f},{top + 140 - v / maximum * 140:.2f}" for t, v in section)
                svg.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2"/>')
                if len(section) == 1:
                    x, y = points.split(',')
                    svg.append(f'<circle cx="{x}" cy="{y}" r="2" fill="{color}"/>')
        svg.append(f'<text x="55" y="{top + 160}" fill="white">0 s</text><text x="700" y="{top + 160}" fill="white">{duration:.2f} s</text>')
    svg.append('<text x="55" y="455" fill="#60a5fa">Pressure</text><text x="170" y="455" fill="#f59e0b">Target pressure</text>'
               '<text x="350" y="455" fill="#34d399">Flow</text><text x="440" y="455" fill="#f472b6">Target flow if available</text>')
    svg.append('</svg>')
    return ''.join(svg)


def report(run):
    run = Path(run)
    events, errors = read_events(run / "lifecycle.jsonl")
    status_path = run / "launcher.json"
    status = json.loads(status_path.read_text()) if status_path.exists() else {}
    errors += status.get("errors", [])
    log = run / "simulator.log"
    if log.exists() and "SIMULATION CAPTURE ERROR:" in log.read_text(errors="replace"):
        errors.append("bridge reported a capture error; see simulator.log")
    errors = list(dict.fromkeys(errors))
    session = events[0].get("session") if events and events[0].get("event") == "run" else None
    if events and (not session or events[0].get("source") != "simulation"):
        raise ValueError("journal is not a simulation run")
    rows = []
    database = run / "snapshots.sqlite"
    if database.exists() and session:
        with closing(live.open_archive(database)) as connection:
            rows = list(live.iter_snapshots(connection, session))
    begins, ends, committed = {}, {}, {}
    for event in events[1:]:
        if event.get("event") == "begin":
            begins[event["shot"]] = event
        elif event.get("event") == "end":
            ends[event["shot"]] = event
        elif event.get("event") == "end_committed":
            committed[event["shot"]] = event["seq"]
    shots, panels = [], []
    for number, begin in sorted(begins.items()):
        end = ends.get(number)
        first = begin["seq"]
        limit = end["seq"] if end else (rows[-1]["seq"] + 1 if rows else first)
        selected = [{"source": "simulation", "shot": number,
                     "elapsed_s": ((r["observation_ms"] - begin["ms"]) & 0xffffffff) / 1000,
                     **r} for r in rows if first <= r["seq"] < limit]
        gaps = missing_ranges(first, limit, selected)
        lost = end["dropped"] - begin["dropped"] if end else None
        if not end or committed.get(number) != end["seq"] or errors:
            state = "open_or_interrupted"
        elif lost or (gaps and status.get("ended")):
            state = "loss"
        elif gaps:
            state = "pending"
        else:
            state = "complete"
        duration = ((end["ms"] - begin["ms"]) & 0xffffffff) / 1000 if end else max([0] + [r["elapsed_s"] for r in selected])
        shot = {"shot": number, "source": "simulation", "state": state, "mode": begin["detail"],
                "first_seq": first, "end_seq_exclusive": limit if end else None,
                "expected": limit - first if end else None, "archived": len(selected),
                "dropped": lost, "missing_ranges": gaps, "duration_s": duration,
                "end_reason": end["detail"] if end else None}
        shots.append(shot)
        stem = f"shot-{number:04d}"
        atomic_text(run / (stem + ".jsonl"), ''.join(json.dumps(r, allow_nan=False) + '\n' for r in selected))
        output = io.StringIO()
        fieldnames = list(selected[0]) if selected else ["source", "shot", "session", "seq", "elapsed_s", "pressure", "flow", "target_pressure", "target_flow"]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(selected)
        atomic_text(run / (stem + ".csv"), output.getvalue())
        svg = plot(selected, duration)
        atomic_text(run / (stem + ".svg"), svg)
        panels.append(f'<section><h2>Simulated shot {number}: {html.escape(shot["mode"])}</h2>'
                      f'<p>Status: <strong>{state}</strong>. Archived {len(selected)} / {shot["expected"]}. '
                      f'Dropped: {lost}. Duration: {duration:.2f} s. End: {html.escape(str(shot["end_reason"]))}.</p>'
                      f'<p><a href="{stem}.csv">CSV</a> · <a href="{stem}.jsonl">JSONL</a> · <a href="{stem}.svg">SVG</a></p>{svg}</section>')
    summary = {"source": "simulation", "hardware_verified": False, "session": session,
               "errors": errors, "shots": shots,
               "units": {"pressure": "bar", "target_pressure": "bar", "flow": "ml/s", "target_flow": "ml/s"},
               "unavailable": ["temperature", "weight", "target_flow"]}
    atomic_text(run / "summary.json", json.dumps(summary, indent=2) + '\n')
    page = ('<!doctype html><html lang="en"><meta charset="utf-8"><title>openPrimus simulated shots</title>' +
            ('<meta http-equiv="refresh" content="2">' if not status.get("ended", True) else '') +
            '<style>body{font:16px system-ui;background:#0f172a;color:#e2e8f0;max-width:900px;margin:40px auto;padding:0 20px}'
            'a{color:#93c5fd}section{margin:35px 0}svg{width:100%}</style><h1>Simulated shots</h1>'
            '<p>Simulation only. Graphs use the immutable WPS1 SQLite archive. Missing sequences break the lines. '
            'Blue: pressure. Orange: target pressure. Green: flow. Pink: target flow if available.</p>'
            '<p>Temperature and weight are absent from WPS1. Target flow is unavailable in this simulator. '
            'This does not verify a WPM machine or firmware patch.</p>' +
            ''.join(f'<p>Capture error: {html.escape(e)}</p>' for e in errors) +
            (''.join(panels) or '<p>Waiting for a simulated shot. Select a mode, then press Start and Stop.</p>') + '</html>')
    atomic_text(run / "report.html", page)
    return summary


def terminate(child, grace=5):
    if child is None:
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        child.wait()
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        child.poll()
        try:
            os.killpg(child.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(.05)
    else:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    child.wait(timeout=5)


def launch(args):
    def interrupted(signum, frame):
        raise KeyboardInterrupt

    previous_handler = signal.signal(signal.SIGTERM, interrupted)
    destination = args.output.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix=time.strftime("%Y%m%d-%H%M%S-"), dir=destination))
    collector = simulator = None
    errors = []
    code = 0
    termination = "simulator_exit"
    atomic_text(run / "launcher.json", json.dumps({"ended": False, "errors": errors}))
    print(f"Simulation output: {run}\nReport: {(run / 'report.html').as_uri()}", flush=True)
    try:
        with (run / "collector.log").open("w") as collector_log, (run / "simulator.log").open("w") as sim_log:
            collector = subprocess.Popen([sys.executable, str(ROOT / 'scripts/kd360s_live.py'), 'serve',
                                          str(run / 'snapshots.sqlite'), '--port', '0'],
                                         stdout=subprocess.PIPE, stderr=collector_log, text=True, start_new_session=True)
            with selectors.DefaultSelector() as selector:
                selector.register(collector.stdout, selectors.EVENT_READ)
                if not selector.select(10):
                    raise RuntimeError("collector did not become ready")
                ready = json.loads(collector.stdout.readline())
            collector_log.write(json.dumps(ready) + '\n')
            collector_log.flush()
            env = {**os.environ, "PRIMUS_SIM_JOURNAL": str(run / 'lifecycle.jsonl'),
                   "PRIMUS_SIM_SESSION": secrets.token_hex(8), "PRIMUS_SIM_PORT": str(ready['listening'][1])}
            env.pop("PRIMUS_SIM_SCENARIO", None)
            if args.scenario:
                env["PRIMUS_SIM_SCENARIO"] = "two-shots"
            command = [str(args.program.resolve())] if args.program else ['bash', str(ROOT / 'scripts/run_sim.sh')]
            simulator = subprocess.Popen(command, cwd=ROOT, env=env, stdout=sim_log,
                                         stderr=subprocess.STDOUT, start_new_session=True)
            while simulator.poll() is None:
                if collector.poll() is not None:
                    raise RuntimeError("collector exited; capture stopped")
                report(run)
                time.sleep(.5)
            code = simulator.returncode
            if code:
                errors.append(f"simulator exited with status {code}")
    except KeyboardInterrupt:
        termination = "interrupted"
        code = 130
    except (OSError, ValueError, RuntimeError) as error:
        errors.append(str(error))
        code = 1
    finally:
        terminate(simulator)
        terminate(collector)
        log = run / 'simulator.log'
        if log.exists() and 'SIMULATION CAPTURE ERROR:' in log.read_text(errors='replace'):
            errors.append("bridge reported a capture error; see simulator.log")
            code = 1
        if not (run / 'lifecycle.jsonl').exists():
            errors.append("simulator did not create a lifecycle journal")
            code = 1
        atomic_text(run / "launcher.json", json.dumps({"ended": True, "termination": termination, "errors": errors}))
        summary = report(run)
        if summary["errors"] or any(shot["state"] != "complete" for shot in summary["shots"]):
            code = code or 1
        if args.scenario and len(summary["shots"]) != 2:
            code = code or 1
        print(json.dumps({"run": str(run), **summary}, indent=2))
        signal.signal(signal.SIGTERM, previous_handler)
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    start = commands.add_parser('run')
    start.add_argument('--output', type=Path, default=ROOT / 'private/simulation')
    start.add_argument('--program', type=Path, help='already compiled SDL executable')
    start.add_argument('--scenario', action='store_true', help='opt-in two shots through the same UI action scripts')
    render = commands.add_parser('report')
    render.add_argument('run', type=Path)
    args = parser.parse_args()
    if args.command == 'report':
        print(json.dumps(report(args.run), indent=2))
        return 0
    return launch(args)


if __name__ == '__main__':
    sys.exit(main())
