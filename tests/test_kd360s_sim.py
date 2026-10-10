"""Exercise the production host bridge against real sockets and durable archives."""
import csv
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import kd360s_live as live
import kd360s_sim as sim

SESSION = 'fedcba9876543210'


class SimulationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix='primus-sim-test-')
        cls.program = Path(cls.build.name) / 'producer'
        compiler = shutil.which('c++')
        if not compiler:
            raise RuntimeError('C++ compiler required for the actual host bridge tests')
        subprocess.run([compiler, '-std=c++17', '-Wall', '-Wextra', '-Werror',
                        '-I', str(ROOT / 'firmware/host'), '-I', str(ROOT / 'experiments/kd360s_snapshot'),
                        str(ROOT / 'tests/sim_capture_driver.cpp'), '-o', str(cls.program)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='primus-sim-run-')
        self.run = Path(self.temp.name)
        self.children = []
        self.now = 0

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
                child.wait(5)
            for pipe in (child.stdin, child.stdout, child.stderr):
                if pipe:
                    pipe.close()
        self.temp.cleanup()

    def collector(self, port=0):
        child = subprocess.Popen([sys.executable, str(ROOT / 'scripts/kd360s_live.py'), 'serve',
                                  str(self.run / 'snapshots.sqlite'), '--port', str(port)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        import selectors
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(5), 'collector readiness timeout')
            ready = json.loads(child.stdout.readline())
        return child, ready['listening'][1]

    def producer(self, port, journal=None, env=None):
        child = subprocess.Popen([str(self.program), str(self.run / 'lifecycle.jsonl' if journal is None else journal), SESSION, str(port)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 env={**os.environ, **(env or {})})
        self.children.append(child)
        return child

    def command(self, child, action, delta=100):
        self.now += delta
        child.stdin.write(f'{action} {self.now}\n')
        child.stdin.flush()
        values = child.stdout.readline().split()
        self.assertEqual(len(values), 4, 'native producer stopped responding')
        self.assertEqual(values[3], 'ok')
        return list(map(int, values[:3]))

    def drain(self, producer):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = self.command(producer, 'poll', 10)
            if state[1] == 0:
                return state
            time.sleep(.003)
        self.fail('host bridge did not drain to durable ACKs')

    def rows(self):
        with closing(live.open_archive(self.run / 'snapshots.sqlite')) as db:
            return list(live.iter_snapshots(db))

    def test_multiple_shots_quick_back_and_exports(self):
        _, port = self.collector()
        producer = self.producer(port)
        self.command(producer, 'observe')
        self.command(producer, 'begin')
        self.command(producer, 'observe')
        self.command(producer, 'end')
        self.command(producer, 'end')
        self.command(producer, 'observe')
        self.command(producer, 'begin')
        self.command(producer, 'back', 0)
        self.drain(producer)
        summary = sim.report(self.run)
        self.assertEqual([s['state'] for s in summary['shots']], ['complete', 'complete'])
        self.assertEqual([s['archived'] for s in summary['shots']], [3, 2])
        self.assertEqual(summary['shots'][1]['duration_s'], 0)
        self.assertEqual(summary['shots'][1]['end_reason'], 'back')
        self.assertEqual([r['seq'] for r in self.rows()], list(range(7)))
        exported = [json.loads(line) for line in (self.run / 'shot-0001.jsonl').read_text().splitlines()]
        self.assertEqual([r['seq'] for r in exported], [1, 2, 3])
        self.assertEqual(exported[0]['pressure'], 9.25)
        self.assertEqual(exported[0]['flow'], 2.5)
        self.assertIsNone(exported[0]['target_flow'])
        self.assertEqual(exported[0]['volume_raw'], 0)
        self.assertEqual(exported[0]['source'], 'simulation')
        with (self.run / 'shot-0001.csv').open() as source:
            csv_rows = list(csv.DictReader(source))
        self.assertEqual([int(r['seq']) for r in csv_rows], [1, 2, 3])
        self.assertEqual(summary['units']['pressure'], 'bar')
        self.assertEqual(summary['units']['flow'], 'ml/s')
        self.assertNotIn('temperature', exported[0])
        self.assertIn('Simulation only', (self.run / 'report.html').read_text())
        self.assertIn('Pressure (bar)', (self.run / 'shot-0001.svg').read_text())

    def test_receiver_restart_preserves_backlog_across_shots(self):
        collector, port = self.collector()
        producer = self.producer(port)
        self.command(producer, 'begin')
        self.drain(producer)
        collector.terminate()
        collector.wait(5)
        self.command(producer, 'observe')
        self.command(producer, 'end')
        self.command(producer, 'begin')
        self.command(producer, 'observe')
        self.command(producer, 'end')
        self.command(producer, 'poll')
        self.assertEqual(sim.report(self.run)['shots'][0]['state'], 'pending')
        self.collector(port)
        self.drain(producer)
        self.assertEqual([r['seq'] for r in self.rows()], list(range(6)))
        self.assertEqual([s['state'] for s in sim.report(self.run)['shots']], ['complete', 'complete'])

    def test_overflow_terminal_drops_and_backlog(self):
        collector, port = self.collector()
        collector.terminate()
        collector.wait(5)
        producer = self.producer(port)
        self.command(producer, 'begin')
        for _ in range(128):
            self.command(producer, 'observe')
        state = self.command(producer, 'end')
        self.assertEqual(state, [130, 128, 2])
        self.assertEqual(sim.report(self.run)['shots'][0]['dropped'], 2)
        self.collector(port)
        self.drain(producer)
        shot = sim.report(self.run)['shots'][0]
        self.assertEqual(shot['state'], 'loss')
        self.assertEqual(shot['archived'], 128)
        self.assertEqual(shot['missing_ranges'], [[128, 130]])
        self.assertEqual(shot['dropped'], 2)
        self.command(producer, 'begin')
        self.command(producer, 'back')
        self.drain(producer)
        self.assertEqual(sim.report(self.run)['shots'][1]['state'], 'complete')

    def test_recovered_shot_plot_does_not_join_overflow_gap(self):
        _, port = self.collector()
        producer = self.producer(port)
        self.command(producer, 'begin')
        for _ in range(129):
            self.command(producer, 'observe')
        self.drain(producer)
        self.command(producer, 'observe')
        self.command(producer, 'end')
        self.drain(producer)
        shot = sim.report(self.run)['shots'][0]
        self.assertEqual(shot['missing_ranges'], [[128, 130]])
        self.assertEqual(shot['state'], 'loss')
        exported = [json.loads(line) for line in (self.run / 'shot-0001.jsonl').read_text().splitlines()]
        self.assertEqual(len(sim.segments(exported, 'pressure')), 2)
        svg = (self.run / 'shot-0001.svg').read_text()
        self.assertEqual(svg.count('<polyline'), 6)
        self.assertIn('SIMULATION', svg)

    def test_wrong_endpoint_identity_ack_and_lost_ack_retry(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as stranger:
            server.bind(('127.0.0.1', 0))
            server.settimeout(1)
            producer = self.producer(server.getsockname()[1])
            self.command(producer, 'begin')
            self.command(producer, 'poll')
            packet, sender = server.recvfrom(1024)
            with closing(live.open_archive(self.run / 'snapshots.sqlite', writable=True)) as db:
                ack = live.record(db, packet, sender)
                stranger.sendto(ack, sender)
                self.assertEqual(self.command(producer, 'poll', 10)[1], 1)
                wrong = live.ACK.pack(b'WPA1', 0, 0)
                server.sendto(wrong, sender)
                self.assertEqual(self.command(producer, 'poll', 10)[1], 1)
                server.sendto(live.ACK.pack(b'WPA1', int(SESSION, 16), 1), sender)
                self.assertEqual(self.command(producer, 'poll', 10)[1], 1)
                server.sendto(ack + b'x', sender)
                self.assertEqual(self.command(producer, 'poll', 10)[1], 1)
                self.command(producer, 'poll', 250)
                replay, _ = server.recvfrom(1024)
                self.assertEqual(packet, replay)
                server.sendto(live.record(db, replay, sender), sender)
                self.drain(producer)
            self.assertEqual(len(self.rows()), 1)

    def test_ack_sends_next_without_waiting_retransmission_deadline(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
            server.bind(('127.0.0.1', 0))
            server.settimeout(1)
            producer = self.producer(server.getsockname()[1])
            self.command(producer, 'begin')
            self.command(producer, 'end')
            self.command(producer, 'poll', 0)
            packet, sender = server.recvfrom(1024)
            server.sendto(live.ACK.pack(b'WPA1', int(SESSION, 16), 0), sender)
            self.command(producer, 'poll', 1)
            deadline = time.monotonic() + 1
            while self.command(producer, 'poll', 0)[1] == 2:
                self.assertLess(time.monotonic(), deadline, 'ACK was not processed')
                time.sleep(.001)
            next_packet, _ = server.recvfrom(1024)
            self.assertEqual(live.decode(next_packet)['seq'], 1)
            self.assertEqual(live.decode(packet)['seq'], 0)

    def test_crash_open_and_closed_unarchived_are_not_complete(self):
        _, port = self.collector()
        producer = self.producer(port)
        self.command(producer, 'begin')
        self.drain(producer)
        producer.kill()
        producer.wait(5)
        self.assertEqual(sim.report(self.run)['shots'][0]['state'], 'open_or_interrupted')
        with (self.run / 'lifecycle.jsonl').open('a') as journal:
            journal.write(json.dumps({'event': 'end', 'shot': 1, 'seq': 3, 'dropped': 0, 'ms': 300, 'detail': 'stop'}) + '\n')
            journal.write(json.dumps({'event': 'end_committed', 'shot': 1, 'seq': 3}) + '\n')
        (self.run / 'launcher.json').write_text(json.dumps({'ended': True}))
        self.assertEqual(sim.report(self.run)['shots'][0]['state'], 'loss')

    def test_end_fsync_failure_never_publishes_committed_marker(self):
        _, port = self.collector()
        producer = self.producer(port, env={'PRIMUS_TEST_FAIL_FSYNC': '3'})
        self.command(producer, 'begin')
        self.drain(producer)
        producer.stdin.write('end 300\n')
        producer.stdin.flush()
        self.assertIn('error', producer.stdout.readline())
        producer.stdin.close()
        producer.wait(5)
        errors = producer.stderr.read()
        self.assertIn('lifecycle journal write failed', errors)
        (self.run / 'simulator.log').write_text(errors)
        events = [json.loads(line) for line in (self.run / 'lifecycle.jsonl').read_text().splitlines()]
        self.assertEqual([e['event'] for e in events], ['run', 'begin', 'end'])
        self.assertEqual(sim.report(self.run)['shots'][0]['state'], 'open_or_interrupted')

    def test_visible_end_without_fsync_confirmation_is_not_complete(self):
        _, port = self.collector()
        producer = self.producer(port)
        self.command(producer, 'begin')
        self.command(producer, 'end')
        self.drain(producer)
        journal = self.run / 'lifecycle.jsonl'
        events = [json.loads(line) for line in journal.read_text().splitlines()]
        self.assertEqual(events[-1]['event'], 'end_committed')
        self.assertEqual(events[-1]['seq'], events[-2]['seq'])
        journal.write_text(''.join(json.dumps(e) + '\n' for e in events if e['event'] != 'end_committed'))
        shot = sim.report(self.run)['shots'][0]
        self.assertEqual(shot['archived'], shot['expected'])
        self.assertEqual(shot['state'], 'open_or_interrupted')

    def test_live_capture_error_invalidates_complete_report(self):
        _, port = self.collector()
        producer = self.producer(port)
        self.command(producer, 'begin')
        self.command(producer, 'end')
        self.drain(producer)
        self.assertEqual(sim.report(self.run)['shots'][0]['state'], 'complete')
        (self.run / 'simulator.log').write_text('SIMULATION CAPTURE ERROR: lifecycle journal write failed\n')
        result = sim.report(self.run)
        self.assertTrue(result['errors'])
        self.assertEqual(result['shots'][0]['state'], 'open_or_interrupted')

    def test_partial_journal_never_reports_complete(self):
        _, port = self.collector()
        producer = self.producer(port)
        self.command(producer, 'begin')
        self.command(producer, 'end')
        self.drain(producer)
        with (self.run / 'lifecycle.jsonl').open('a') as journal:
            journal.write('{"event":')
        result = sim.report(self.run)
        self.assertTrue(result['errors'])
        self.assertEqual(result['shots'][0]['state'], 'open_or_interrupted')

    def test_report_refresh_only_while_launcher_runs(self):
        status = self.run / 'launcher.json'
        status.write_text(json.dumps({'ended': False}))
        sim.report(self.run)
        self.assertIn('http-equiv="refresh"', (self.run / 'report.html').read_text())
        status.write_text(json.dumps({'ended': True}))
        sim.report(self.run)
        self.assertNotIn('http-equiv="refresh"', (self.run / 'report.html').read_text())

    def test_plot_splits_missing_sequences_and_nonfinite_values(self):
        rows = [{'seq': i, 'elapsed_s': i / 10, 'pressure': value} for i, value in [(0, 1.), (1, 2.), (3, 3.), (4, None), (5, 4.)]]
        self.assertEqual(sim.segments(rows, 'pressure'), [[(0, 1.), (.1, 2.)], [(.3, 3.)], [(.5, 4.)]])

    def test_launcher_interrupt_preserves_completed_shot(self):
        output = self.run / 'launcher-runs'
        launcher = subprocess.Popen([sys.executable, str(ROOT / 'scripts/kd360s_sim.py'), 'run',
                                     '--program', str(self.program), '--output', str(output)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.children.append(launcher)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                summaries = list(output.glob('*/summary.json'))
                if summaries:
                    summary = json.loads(summaries[0].read_text())
                    if summary['shots'] and summary['shots'][0]['state'] == 'complete':
                        break
                time.sleep(.05)
            else:
                self.fail('launcher did not capture the native production bridge shot')
            launcher.send_signal(signal.SIGINT)
            stdout, stderr = launcher.communicate(timeout=10)
            self.assertEqual(launcher.returncode, 130, stderr + stdout)
            summary = json.loads(summaries[0].read_text())
            self.assertEqual(summary['shots'][0]['state'], 'complete')
            self.assertEqual(summary['errors'], [])
            status = json.loads((summaries[0].parent / 'launcher.json').read_text())
            self.assertEqual(status['termination'], 'interrupted')
            self.assertTrue(status['ended'])
        finally:
            if launcher.poll() is None:
                launcher.send_signal(signal.SIGTERM)
                launcher.wait(10)

    @unittest.skipUnless(os.name == 'posix', 'owned process groups require POSIX')
    def test_cleanup_kills_descendants_even_after_group_leader_exits(self):
        child_code = ('import os,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                      'print(os.getpid(), flush=True); time.sleep(60)')
        for leader_exits in (False, True):
            with self.subTest(leader_exits=leader_exits):
                leader_code = ('import subprocess,sys,time; '
                               f'p=subprocess.Popen([sys.executable,"-c",{child_code!r}], stdout=subprocess.PIPE, text=True); '
                               'print(p.stdout.readline(), end="", flush=True); ' +
                               ('' if leader_exits else 'time.sleep(60)'))
                leader = subprocess.Popen([sys.executable, '-c', leader_code], stdout=subprocess.PIPE,
                                          text=True, start_new_session=True)
                self.children.append(leader)
                try:
                    descendant = int(leader.stdout.readline())
                    if leader_exits:
                        leader.wait(5)
                    sim.terminate(leader, grace=.1)
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        try:
                            os.kill(descendant, 0)
                        except ProcessLookupError:
                            break
                        time.sleep(.02)
                    else:
                        self.fail('owned descendant survived process group cleanup')
                finally:
                    sim.terminate(leader, grace=.1)

    def test_disabled_bridge_creates_no_capture(self):
        producer = self.producer(47653, journal='')
        for command in ('begin', 'observe', 'end', 'poll'):
            self.assertEqual(self.command(producer, command), [0, 0, 0])
        self.assertFalse((self.run / 'lifecycle.jsonl').exists())

    def test_capture_error_is_visible_and_journal_is_not_overwritten(self):
        journal = self.run / 'lifecycle.jsonl'
        journal.write_text('existing\n')
        producer = self.producer(47653)
        producer.stdin.write('begin 1\n')
        producer.stdin.flush()
        self.assertIn('error', producer.stdout.readline())
        producer.terminate()
        producer.wait(5)
        self.assertIn('SIMULATION CAPTURE ERROR', producer.stderr.read())
        self.assertEqual(journal.read_text(), 'existing\n')


if __name__ == '__main__':
    unittest.main()
