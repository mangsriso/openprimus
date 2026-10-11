import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('cc'), 'host C compiler required')
class SnapshotRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='kd360s-runtime-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.binary = pathlib.Path(cls.temporary.name) / 'runtime'
        subprocess.run(['cc', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                        str(ROOT / 'tests/snapshot_runtime_driver.c'), '-o', str(cls.binary)], check=True)

    def run_case(self, case):
        result = subprocess.run([str(self.binary), case], check=True, capture_output=True,
                                text=True, timeout=5)
        self.assertEqual(result.stdout, f'{case} passed\n')

    def test_inline_heap_credential_boundaries(self):
        self.run_case('strings')

    def test_allocation_task_and_credentials_fallback_to_stock_loop(self):
        self.run_case('fallback')

    def test_stock_call_order_observation_interval_raw_fields_and_clock_wrap(self):
        self.run_case('observation')

    def test_failed_boot_begin_retries_with_ten_second_backoff_and_retains_credentials(self):
        self.run_case('begin-retry')

    def test_disconnect_socket_and_send_failures_preserve_pending_records(self):
        self.run_case('reconnect')

    def test_ack_sender_identity_length_and_duplicate_validation(self):
        self.run_case('ack-validation')

    def test_window_record_retries_at_250ms_including_clock_wrap(self):
        self.run_case('retry')

    def test_hook_lifecycle_repeated_acquisitions_context_and_loss(self):
        self.run_case('lifecycle')

    def test_idle_does_not_fill_queue_and_stop_survives_telemetry_saturation(self):
        self.run_case('idle-retention')

    def test_external_buffer_released_only_before_context_publication(self):
        self.run_case('retention-lifetime')

    def test_window_batches_delayed_out_of_order_acks_and_partial_send_failure(self):
        self.run_case("window-delivery")

    def test_wifi_owner_screen_service_flags_ap_mode_and_current_credentials(self):
        self.run_case('wifi-ownership')

    def test_worker_attachment_failure_disables_capture_and_preserves_single_producer(self):
        self.run_case('worker-attachment')
