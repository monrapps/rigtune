import copy
import io
import json
import logging
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from rigtune.config import ConfigError
from rigtune.hiveos import (
    DEFAULTS, HiveClient, HiveError, HiveSync, build_patch, load_settings,
    load_snapshot, pci_address, read_token, serve, verify_local_worker,
)
from tests.helpers import FakeBackend, UUID_A, UUID_B, sample


NOW = 1000


def gpu(uuid=UUID_A, bus='00000000:01:00.0', power=130):
    return dict(uuid=uuid, pci_bus_id=bus, power_limit_w=power, power_min_w=100,
                power_max_w=200, temperature_c=60, critical_temp_c=75,
                recovering=False, state='in_target_band')


def snapshot():
    return dict(version=1, updated_at=NOW, healthy=True, gpus=[gpu()])


def worker():
    return dict(
        id=42, farm_id=7, stats={'online': True, 'stats_time': NOW}, commands=[],
        gpu_info=[
            {'index': 0, 'brand': 'internal', 'bus_id': '00:02.0'},
            {'index': 2, 'brand': 'nvidia', 'bus_id': '02:00.0', 'uuid': UUID_B},
            {'index': 1, 'brand': 'nvidia', 'bus_id': '01:00.0', 'uuid': UUID_A},
        ],
        overclock={'nvidia': {'power_limit': '120 140'}},
        oc_config={'default': {'nvidia': {'power_limit': '120 140', 'mem_clock': '-2000',
                                        'fan_speed': '100'}, 'amd': {'core_clock': '1200'}},
                   'by_algo': [{'algo': 'other', 'nvidia': {'power_limit': '150 160'}}]},
    )


class FakeClient:
    def __init__(self, data=None):
        self.worker = data or worker()
        self.patches = []

    def get_worker(self):
        return copy.deepcopy(self.worker)

    def update_worker(self, body):
        self.patches.append(copy.deepcopy(body))
        if 'default' in body['oc_config']:
            self.worker['oc_config']['default']['nvidia'].update(body['oc_config']['default']['nvidia'])
        else:
            self.worker['oc_config']['by_algo'] = copy.deepcopy(body['oc_config']['by_algo'])
        self.worker['commands'] = [{'id': 1, 'command': 'nvidia_oc'}]
        return {'id': 1}


class HiveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = self.directory.name
        self.settings = dict(DEFAULTS, farm_id=7, worker_id=42,
                             token_file=os.path.join(root, 'hiveos.token'),
                             status_file=os.path.join(root, 'status.json'),
                             rig_config_file=os.path.join(root, 'rig.conf'))
        self.write(self.settings['token_file'], 'test-secret')
        os.chmod(self.settings['token_file'], 0o600)
        self.write(self.settings['rig_config_file'], 'RIG_ID=42\nRIG_PASSWD=never-log-this\n')
        self.save_snapshot(snapshot())

    def write(self, path, value):
        with open(path, 'w') as handle:
            handle.write(value)

    def save_snapshot(self, value):
        self.write(self.settings['status_file'], json.dumps(value))

    def config_path(self, **overrides):
        path = os.path.join(self.directory.name, 'hiveos.json')
        data = dict(self.settings, **overrides)
        self.write(path, json.dumps(data))
        return path

    def sync(self, client=None):
        return HiveSync(self.settings, client=client or FakeClient(), backend=FakeBackend(), clock=lambda: NOW)

    def test_settings_reject_secrets_in_json_and_insecure_urls(self):
        self.assertEqual(load_settings(self.config_path())['sync_interval_s'], 60)
        for values in [{'token': 'do-not-store-here'}, {'worker_id': True}, {'farm_id': 0},
                       {'sync_interval_s': 5}, {'retry_max_s': 59}, {'request_timeout_s': float('nan')},
                       {'api_url': 'http://api.example.test'}, {'api_url': 'https://user:secret@api.test'},
                       {'api_url': 'https://api.test?token=value'}, {'token_file': 'relative'},
                       {'max_status_age_s': 1000}]:
            with self.subTest(values=values), self.assertRaises(ConfigError):
                load_settings(self.config_path(**values))

    def test_token_file_permissions_rotation_and_redirection(self):
        self.assertEqual(read_token(self.settings['token_file']), 'test-secret')
        self.write(self.settings['token_file'], 'rotated-secret\n')
        self.assertEqual(read_token(self.settings['token_file']), 'rotated-secret')
        os.chmod(self.settings['token_file'], 0o644)
        with self.assertRaises(HiveError):
            read_token(self.settings['token_file'])
        os.chmod(self.settings['token_file'], 0o600)
        target = os.path.join(self.directory.name, 'alias.token')
        os.symlink(self.settings['token_file'], target)
        with self.assertRaises(HiveError):
            read_token(target)

    def test_token_rejects_header_injection_and_non_ascii(self):
        for token in ['', 'Bearer token', 'token\nHeader: x', '\u00e9token', 'x' * 4097]:
            with self.subTest(length=len(token)):
                self.write(self.settings['token_file'], token)
                with self.assertRaises(HiveError):
                    read_token(self.settings['token_file'])

    def test_local_worker_identity_is_required(self):
        verify_local_worker(self.settings)
        for text in ['RIG_ID=41\n', 'RIG_ID=42; touch /tmp/example\n', 'RIG_ID=42\nRIG_ID=42\n']:
            self.write(self.settings['rig_config_file'], text)
            with self.assertRaises(HiveError):
                verify_local_worker(self.settings)

    def test_status_rejects_stale_unhealthy_missing_and_unsafe_data(self):
        bad = [dict(updated_at=NOW - 21), dict(updated_at=NOW + 10), dict(healthy=False),
               dict(gpus=[]), dict(updated_at=float('nan'))]
        for change in bad:
            self.save_snapshot(dict(snapshot(), **change))
            with self.subTest(change=change), self.assertRaises(HiveError):
                load_snapshot(self.settings, NOW)
        for change in [{'power_limit_w': None}, {'power_limit_w': 201}, {'power_limit_w': 130.5},
                       {'recovering': True}, {'temperature_c': 75}, {'uuid': 'GPU-short'},
                       {'pci_bus_id': 'bad'}, {'temperature_c': True}]:
            data = snapshot()
            data['gpus'][0].update(change)
            self.save_snapshot(data)
            with self.subTest(change=change), self.assertRaises(HiveError):
                load_snapshot(self.settings, NOW)

    def test_pci_normalization_preserves_domain(self):
        self.assertEqual(pci_address('00000000:0A:00.0'), pci_address('0a:00.0'))
        self.assertNotEqual(pci_address('0001:01:00.0'), pci_address('01:00.0'))

    def test_maps_uuid_to_pci_not_nvidia_index_and_preserves_unmanaged_cards(self):
        original = worker()
        body = build_patch(original, snapshot(), self.settings, NOW)
        self.assertEqual(body, {'oc_apply_mode': 'merge', 'oc_config': {
            'default': {'nvidia': {'power_limit': '130 140'}}}})
        self.assertEqual(original, worker())

    def test_single_power_value_expands_using_hiveos_last_value_rule(self):
        data = worker()
        data['oc_config']['default']['nvidia']['power_limit'] = '120'
        body = build_patch(data, snapshot(), self.settings, NOW)
        self.assertEqual(body['oc_config']['default']['nvidia']['power_limit'], '130 120')

    def test_all_gpus_are_batched_into_one_update(self):
        data = snapshot()
        data['gpus'].append(gpu(UUID_B, '00000000:02:00.0', 150))
        body = build_patch(worker(), data, self.settings, NOW)
        self.assertEqual(body['oc_config']['default']['nvidia']['power_limit'], '130 150')

    def test_active_algorithm_preserves_default_other_algorithms_and_fields(self):
        data = worker()
        data['overclock']['algo'] = 'active'
        data['oc_config']['by_algo'].append({'algo': 'active', 'nvidia': {
            'power_limit': '120 140', 'mem_clock': '900'}, 'tweakers': {'tool': {'x': '1'}}})
        before = copy.deepcopy(data)
        body = build_patch(data, snapshot(), self.settings, NOW)
        self.assertNotIn('default', body['oc_config'])
        self.assertEqual(body['oc_config']['by_algo'][0], before['oc_config']['by_algo'][0])
        self.assertEqual(body['oc_config']['by_algo'][1]['nvidia'], {'power_limit': '130 140', 'mem_clock': '900'})
        self.assertEqual(body['oc_config']['by_algo'][1]['tweakers'], {'tool': {'x': '1'}})
        self.assertEqual(data, before)

    def test_unknown_active_algorithm_fails_closed(self):
        data = worker()
        data['overclock']['algo'] = 'unknown'
        with self.assertRaises(HiveError):
            build_patch(data, snapshot(), self.settings, NOW)

    def test_inherited_algorithm_power_preserves_uncontrolled_gpu(self):
        data = worker()
        data['overclock']['algo'] = 'active'
        data['oc_config']['by_algo'].append({'algo': 'active', 'nvidia': {'mem_clock': '900'}})
        body = build_patch(data, snapshot(), self.settings, NOW)
        self.assertEqual(body['oc_config']['by_algo'][1]['nvidia']['power_limit'], '130 140')

    def test_unspecified_uncontrolled_power_cannot_be_reset_to_factory_default(self):
        data = worker()
        data['oc_config']['default']['nvidia']['power_limit'] = ''
        data['overclock']['nvidia'] = {}
        with self.assertRaisesRegex(HiveError, 'uncontrolled'):
            build_patch(data, snapshot(), self.settings, NOW)

    def test_offline_wrong_worker_and_queued_commands_block_update(self):
        for change in [{'id': 43}, {'farm_id': 8}, {'stats': {'online': False, 'stats_time': NOW}},
                       {'stats': {'online': True, 'stats_time': NOW - 121}}, {'commands': [{'id': 99}]}]:
            data = worker()
            data.update(change)
            with self.subTest(change=change), self.assertRaises(HiveError):
                build_patch(data, snapshot(), self.settings, NOW)

    def test_ambiguous_missing_or_mismatched_gpu_blocks_update(self):
        for kind in ['missing', 'duplicate_bus', 'duplicate_index', 'wrong_uuid', 'no_index']:
            data = worker()
            if kind == 'missing':
                data['gpu_info'] = []
            elif kind == 'duplicate_bus':
                data['gpu_info'][1]['bus_id'] = '01:00.0'
            elif kind == 'duplicate_index':
                data['gpu_info'][1]['index'] = 1
            elif kind == 'wrong_uuid':
                data['gpu_info'][2]['uuid'] = UUID_B
            else:
                del data['gpu_info'][2]['index']
            with self.subTest(kind=kind), self.assertRaises(HiveError):
                build_patch(data, snapshot(), self.settings, NOW)

    def test_sync_reads_back_update_and_does_not_repeat_it(self):
        sync = self.sync()
        sync.cycle()
        sync.cycle()
        self.assertEqual(len(sync.client.patches), 1)
        self.assertEqual(sync.backend.writes, [])

    def test_dry_run_only_reads_api_and_hardware(self):
        sync = self.sync()
        with self.assertLogs('rigtune.hiveos', level='INFO'):
            result = sync.cycle(dry_run=True)
        self.assertIsNotNone(result)
        self.assertEqual(sync.client.patches, [])
        self.assertEqual(sync.backend.writes, [])

    def test_changed_hardware_or_new_protection_prevents_update(self):
        for reading in [sample(power=129), sample(temp=75), sample(uuid=UUID_B)]:
            sync = self.sync()
            sync.backend.readings = [reading]
            with self.assertRaises(HiveError):
                sync.cycle()
            self.assertEqual(sync.client.patches, [])
        sync = self.sync()
        original_read = sync.backend.read_all
        def fail_after_read():
            self.save_snapshot(dict(snapshot(), healthy=False))
            return original_read()
        sync.backend.read_all = fail_after_read
        with self.assertRaises(HiveError):
            sync.cycle()
        self.assertEqual(sync.client.patches, [])

    def test_old_snapshot_is_not_reused_after_slow_api_get(self):
        sync = self.sync()
        def slow_get():
            self.save_snapshot(dict(snapshot(), updated_at=NOW - 30))
            return worker()
        sync.client.get_worker = slow_get
        with self.assertRaises(HiveError):
            sync.cycle()
        self.assertEqual(sync.client.patches, [])

    def test_failed_remote_readback_is_not_reported_as_success(self):
        sync = self.sync()
        sync.client.update_worker = Mock(return_value={'id': 9})
        with self.assertRaisesRegex(HiveError, 'readback'):
            sync.cycle()

    def test_api_errors_are_bounded_and_do_not_disclose_secrets(self):
        opener = Mock()
        for error in [HTTPError('https://api.test', code, 'test-secret', {}, io.BytesIO(b'test-secret'))
                      for code in (401, 403, 429, 500)]:
            opener.open.side_effect = error
            client = HiveClient(self.settings, opener=opener)
            with self.assertRaises(HiveError) as caught:
                client.get_worker()
            self.assertNotIn('test-secret', str(caught.exception))
            self.assertIn(str(error.code), str(caught.exception))
        opener.open.side_effect = URLError('test-secret')
        with self.assertRaises(HiveError) as caught:
            HiveClient(self.settings, opener=opener).get_worker()
        self.assertNotIn('test-secret', str(caught.exception))
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 10)

    def test_backoff_and_once_exit_status(self):
        sync, stop = Mock(), Mock()
        sync.cycle.side_effect = HiveError('offline')
        stop.is_set.side_effect = [False, False, False, False, False, True]
        settings = dict(self.settings, retry_max_s=240)
        with self.assertLogs('rigtune.hiveos', level='WARNING'):
            self.assertEqual(serve(sync, settings, stop), 0)
        self.assertEqual([call.args[0] for call in stop.wait.call_args_list], [60, 120, 240, 240, 240])
        with self.assertLogs('rigtune.hiveos', level='WARNING'):
            self.assertEqual(serve(sync, settings, threading.Event(), once=True), 1)
        sync.cycle.side_effect = None
        self.assertEqual(serve(sync, settings, threading.Event(), once=True), 0)

    def test_free_farm_restriction_has_a_safe_actionable_error(self):
        opener = Mock()
        body = json.dumps({'message': 'Cannot use API keys to access free farms', 'secret': 'test-secret'})
        opener.open.side_effect = HTTPError('https://api.test', 403, 'Forbidden', {}, io.BytesIO(body.encode()))
        with self.assertRaisesRegex(HiveError, 'free farm') as caught:
            HiveClient(self.settings, opener=opener).get_worker()
        self.assertNotIn('test-secret', str(caught.exception))

    def test_network_timeout_is_reported_without_request_details(self):
        opener = Mock()
        opener.open.side_effect = TimeoutError('test-secret')
        with self.assertRaisesRegex(HiveError, 'request failed') as caught:
            HiveClient(self.settings, opener=opener).get_worker()
        self.assertNotIn('test-secret', str(caught.exception))

    def test_real_http_protocol_and_redirect_refusal(self):
        state, requests = FakeClient(), []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def reply(self, value):
                body = json.dumps(value).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def do_GET(self):
                requests.append((self.command, self.path, self.headers.get('Authorization')))
                if self.path.endswith('/redirect'):
                    self.send_response(302)
                    self.send_header('Location', '/must-not-follow')
                    self.end_headers()
                else:
                    self.reply(state.get_worker())
            def do_PATCH(self):
                requests.append((self.command, self.path, self.headers.get('Authorization')))
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                self.reply(state.update_worker(body))
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            # The production config parser forbids HTTP; only this fake server uses it.
            settings = dict(self.settings, api_url='http://127.0.0.1:{}'.format(server.server_port))
            sync = self.sync(client=HiveClient(settings))
            sync.cycle()
            self.assertEqual([entry[0] for entry in requests], ['GET', 'PATCH', 'GET'])
            self.assertTrue(all(entry[2] == 'Bearer test-secret' for entry in requests))
            with self.assertRaisesRegex(HiveError, '302'):
                sync.client.request('GET', '/redirect')
            self.assertEqual(requests[-1][1], '/redirect')
            self.assertFalse(any(entry[1] == '/must-not-follow' for entry in requests))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
