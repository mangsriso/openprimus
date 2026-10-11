from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import kd360s_build_snapshot as build
from kd360s_firmware import FirmwareError


class MarkerTests(unittest.TestCase):
    def payload(self, **changes):
        fields = dict(name='recorder_key_marker', section_name='.text', info=2,
                      address=0x42106ea0, size=5)
        fields.update(changes)
        return SimpleNamespace(symbols=[SimpleNamespace(**fields)], sections=[
            SimpleNamespace(name='.text', address=0x42106ea0, data=bytes.fromhex('3641001df0000000'))])

    def test_distinct_inert_marker_and_invalid_metadata(self):
        with patch.object(build, 'parse_elf', return_value=self.payload()):
            build.verify_marker(b'elf')
        for fields in ({'size': 0}, {'size': 6}, {'address': 0x42106ea1},
                       {'address': 0x42106ea8}, {'section_name': '.rodata'}, {'info': 1}):
            with self.subTest(fields=fields), patch.object(build, 'parse_elf', return_value=self.payload(**fields)):
                with self.assertRaises(FirmwareError):
                    build.verify_marker(b'elf')
        for case in ('missing', 'duplicate', 'alias', 'side_effect'):
            payload = self.payload()
            if case == 'missing':
                payload.symbols.clear()
            elif case == 'duplicate':
                payload.symbols *= 2
            elif case == 'alias':
                payload.symbols.append(SimpleNamespace(name='other', info=2, address=0x42106ea0))
            else:
                payload.sections[0].data = bytes.fromhex('3641000c02000000')
            with self.subTest(case=case), patch.object(build, 'parse_elf', return_value=payload):
                with self.assertRaises(FirmwareError):
                    build.verify_marker(b'elf')
