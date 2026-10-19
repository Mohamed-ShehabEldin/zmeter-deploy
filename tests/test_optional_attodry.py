"""Synthetic files and fake subprocesses only; no network, Conda or devices."""
import hashlib
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

import deploy_zmeter as deploy


class OptionalAttoDryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        # Synthetic bytes, not the licensed vendor package.
        self.payload = b'fake vendor wheel for offline unit tests only'
        patcher = mock.patch.object(deploy, 'ATTO_DEVICE_WHEEL_SHA256', hashlib.sha256(self.payload).hexdigest())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.environment = deploy.EnvironmentState('zmeter-v1.0-beta.99', False)

    def wheel(self, relative=None):
        path = self.root / (relative or deploy.ATTO_DEVICE_VENDOR_WHEEL)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.payload)
        return path

    def test_detects_only_maintained_package_path(self):
        for name, expected in [('devices/attodry/attodry_main.py', True), ('devices/attodry0/attodry_main.py', False),
                               ('devices/mockDevice/main.py', False)]:
            package = SimpleNamespace(members=(zipfile.ZipInfo(name),))
            self.assertEqual(deploy._package_contains_attodry(package), expected)

    def test_find_adjacent_wheel_and_stage_with_standard_filename(self):
        source = self.wheel()
        self.assertEqual(deploy.find_attodry_vendor_wheel(self.root), source)
        staging = self.root / 'staging'
        staging.mkdir()
        staged = deploy.stage_attodry_vendor_wheel(source, staging)
        self.assertEqual(staged.name, 'atto_device-1.12.0-py3-none-any.whl')
        self.assertEqual(staged.read_bytes(), self.payload)
        self.assertEqual(source.read_bytes(), self.payload)

    def test_find_usb_tree_and_canonical_filename(self):
        source = self.wheel(Path('Attocube/u drive/Software/API') / deploy.ATTO_DEVICE_INSTALL_WHEEL)
        self.assertEqual(deploy.find_attodry_vendor_wheel(self.root), source)

    def test_missing_and_bad_wheels_fail_clearly(self):
        with self.assertRaisesRegex(FileNotFoundError, 'vendor-supplied'):
            deploy.find_attodry_vendor_wheel(self.root)
        source = self.wheel()
        source.write_bytes(b'wrong version')
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            deploy.find_attodry_vendor_wheel(self.root)

    def test_stage_rechecks_hash_and_cleans_invalid_copy(self):
        source = self.wheel()
        source.write_bytes(b'changed after discovery')
        staging = self.root / 'staging'
        staging.mkdir()
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            deploy.stage_attodry_vendor_wheel(source, staging)
        self.assertFalse((staging / deploy.ATTO_DEVICE_INSTALL_WHEEL).exists())

    def test_skip_never_searches_for_a_wheel(self):
        for answer in ('', 'n', 'no'):
            with mock.patch.object(deploy, '_read', return_value=answer), mock.patch.object(deploy, 'find_attodry_vendor_wheel') as find:
                self.assertIsNone(deploy.select_attodry_vendor_wheel(self.root))
                find.assert_not_called()

    def test_opt_in_discovers_and_verifies_local_wheel(self):
        source = self.wheel()
        with mock.patch.object(deploy, '_read', return_value='yes'):
            self.assertEqual(deploy.select_attodry_vendor_wheel(self.root), source)

    def test_opt_in_accepts_verified_explicit_path_or_cancels(self):
        source = self.wheel(Path('elsewhere') / deploy.ATTO_DEVICE_VENDOR_WHEEL)
        with mock.patch.object(deploy, '_read', side_effect=['y', f'"{source}"']):
            self.assertEqual(deploy.select_attodry_vendor_wheel(self.root), source)
        with mock.patch.object(deploy, '_read', side_effect=['y', '']):
            with self.assertRaises(deploy.UserCancelled):
                deploy.select_attodry_vendor_wheel(self.root)

    def test_pip_command_uses_exact_release_environment_and_no_index(self):
        command = deploy.build_conda_pip_install_command('conda.exe', self.environment, self.root / deploy.ATTO_DEVICE_INSTALL_WHEEL)
        self.assertEqual(command[:8], ('conda.exe', 'run', '--name', self.environment.name, 'python', '-m', 'pip', 'install'))
        self.assertIn('--no-index', command)
        self.assertIn('--no-deps', command)

    def execute_fake_setup(self, driver=True, wheel=None, return_codes=(0, 0)):
        env_file = self.root / 'environment.yml'
        env_file.write_text('name: synthetic\n', encoding='utf-8')
        package = SimpleNamespace(environment_path=env_file, members=(zipfile.ZipInfo(
            'devices/attodry/attodry_main.py' if driver else 'devices/mockDevice/main.py'),))
        client = SimpleNamespace(resolve_tag_commit=mock.Mock(return_value='a' * 40))
        release = SimpleNamespace(tag='v1.0-beta.99', asset=SimpleNamespace(name='synthetic.zip', size=1))
        runner = mock.Mock(side_effect=[SimpleNamespace(returncode=code) for code in return_codes])
        self.runner = runner
        with (mock.patch.object(deploy, 'download_release_asset'), mock.patch.object(deploy, 'verify_release_package', return_value=package),
              mock.patch.object(deploy, 'select_attodry_vendor_wheel', return_value=wheel) as selector):
            deploy.execute_environment_setup(client, release, 'conda.exe', self.environment, progress=mock.Mock(),
                                             runner=runner, installer_directory=self.root)
        return runner, selector

    def test_non_attodry_release_does_not_prompt_or_run_pip(self):
        runner, selector = self.execute_fake_setup(driver=False)
        selector.assert_not_called()
        self.assertEqual(runner.call_count, 1)

    def test_skipped_optional_package_still_creates_environment(self):
        runner, selector = self.execute_fake_setup()
        selector.assert_called_once()
        self.assertEqual(runner.call_count, 1)

    def test_opted_in_package_installs_after_conda_in_exact_environment(self):
        runner, _ = self.execute_fake_setup(wheel=self.wheel())
        self.assertEqual(runner.call_count, 2)
        commands = [call.args[0] for call in runner.call_args_list]
        self.assertEqual(commands[0][1:5], ['env', 'create', '--name', self.environment.name])
        self.assertEqual(commands[1][1:4], ['run', '--name', self.environment.name])
        self.assertEqual(Path(commands[1][-1]).name, deploy.ATTO_DEVICE_INSTALL_WHEEL)

    def test_conda_failure_prevents_pip_and_pip_failure_is_not_success(self):
        with self.assertRaises(deploy.CondaCommandError):
            self.execute_fake_setup(wheel=self.wheel(), return_codes=(1,))
        self.assertEqual(self.runner.call_count, 1)
        with self.assertRaises(deploy.CondaCommandError):
            self.execute_fake_setup(wheel=self.wheel(), return_codes=(0, 2))
        self.assertEqual(self.runner.call_count, 2)

    def test_invalid_wheel_fails_before_any_conda_mutation(self):
        source = self.wheel()
        source.write_bytes(b'bad')
        with self.assertRaises(ValueError):
            self.execute_fake_setup(wheel=source)
        self.runner.assert_not_called()


if __name__ == '__main__':
    unittest.main()
