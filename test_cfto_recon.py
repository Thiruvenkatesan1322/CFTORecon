from contextlib import ExitStack, redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import shlex
import socket
import sys
from tempfile import TemporaryDirectory
import types
import unittest
from unittest.mock import MagicMock, Mock, patch

import paramiko

import CFTORecon as recon


class JumpConnectionTests(unittest.TestCase):
    def test_network_failures_are_distinguished(self):
        failures = [
            (socket.gaierror("DNS failure"), "Cannot resolve SSH jump host"),
            (socket.timeout("TCP timeout"), "the Oracle database has not been contacted"),
            (ConnectionRefusedError("Refused"), "Cannot open SSH TCP connection"),
        ]
        for failure, message in failures:
            with self.subTest(failure=type(failure).__name__):
                with patch.object(recon.socket, "create_connection", side_effect=failure):
                    with self.assertRaisesRegex(RuntimeError, message) as captured:
                        recon.open_jump_socket()
                    self.assertIs(captured.exception.__cause__, failure)

    def test_check_ssh_does_not_require_database_credentials(self):
        connection_socket = Mock()
        output = StringIO()
        with patch.object(recon, "open_jump_socket", return_value=connection_socket):
            with patch.object(recon, "fetch_sterling_orders") as fetch, redirect_stdout(output):
                self.assertEqual(recon.main(["--check-ssh"]), 0)
                fetch.assert_not_called()
        connection_socket.close.assert_called_once_with()
        self.assertIn("Oracle database were not checked", output.getvalue())

    def test_check_ssh_returns_failure_without_traceback(self):
        output = StringIO()
        with patch.object(recon, "open_jump_socket", side_effect=RuntimeError("TCP blocked")):
            with redirect_stderr(output):
                self.assertEqual(recon.main(["--check-ssh"]), 1)
        self.assertIn("FAILED: TCP blocked", output.getvalue())


class SterlingFetchTests(unittest.TestCase):
    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        temporary_directory = self.patches.enter_context(TemporaryDirectory())
        self.client = Mock()
        self.connection_socket = Mock()
        self.client.get_transport.return_value.is_active.return_value = True
        self.stdin = Mock()
        self.stdout = Mock()
        self.stderr = Mock()
        self.client.exec_command.return_value = self.stdin, self.stdout, self.stderr
        self.set_response({"orders": [" 000123 ", "00456", None]})
        self.patches.enter_context(patch.object(recon, "STERLING_DB_USER", "TEST_USER"))
        self.patches.enter_context(patch.object(recon, "STERLING_DB_PASSWORD", "test-only-password"))
        self.patches.enter_context(patch.object(recon, "KEY_PATH", __file__))
        self.patches.enter_context(patch.object(
            recon, "KNOWN_HOSTS", Path(temporary_directory) / "known_hosts"))
        self.patches.enter_context(patch.object(paramiko, "SSHClient", return_value=self.client))
        self.socket_opener = self.patches.enter_context(patch.object(
            recon, "open_jump_socket", return_value=self.connection_socket))
        self.patches.enter_context(patch.dict(sys.modules, {"oracledb": None}))

    def set_response(self, result, status=0):
        self.stdout.read.return_value = json.dumps(result).encode("utf-8")
        self.stderr.read.return_value = b""
        self.stdout.channel.recv_exit_status.return_value = status

    def assert_resources_closed(self):
        self.client.close.assert_called_once_with()
        self.connection_socket.close.assert_called_once_with()

    def test_fetch_runs_query_on_jump_server_with_credentials_on_stdin(self):
        self.assertEqual(recon.fetch_sterling_orders(), ["000123", "00456"])
        command = self.client.exec_command.call_args.args[0]
        self.assertEqual(shlex.split(command), [recon.JUMP_PYTHON, "-c", recon.REMOTE_QUERY_SCRIPT])
        self.assertNotIn(recon.STERLING_DB_PASSWORD, command)
        settings = json.loads(self.stdin.write.call_args.args[0])
        self.assertEqual(settings["user"], recon.STERLING_DB_USER)
        self.assertEqual(settings["password"], recon.STERLING_DB_PASSWORD)
        self.assertEqual(settings["dsn"], recon.STERLING_DSN)
        self.assertEqual(settings["query"], recon.STERLING_RELEASE_QUERY)
        self.assertIs(self.client.connect.call_args.kwargs["sock"], self.connection_socket)
        self.assertEqual(self.client.connect.call_args.kwargs["port"], 22)
        self.stdin.flush.assert_called_once_with()
        self.stdin.channel.shutdown_write.assert_called_once_with()
        self.client.get_transport.return_value.open_channel.assert_not_called()
        self.assert_resources_closed()

    def test_authentication_failure_prevents_database_connection(self):
        self.client.connect.side_effect = paramiko.AuthenticationException("Denied")
        with self.assertRaisesRegex(RuntimeError, "SSH authentication failed"):
            recon.fetch_sterling_orders()
        self.client.exec_command.assert_not_called()
        self.assert_resources_closed()

    def test_handshake_timeout_is_not_reported_as_database_failure(self):
        self.client.connect.side_effect = socket.timeout("Handshake timed out")
        with self.assertRaisesRegex(RuntimeError, "TCP connected"):
            recon.fetch_sterling_orders()
        self.client.exec_command.assert_not_called()
        self.assert_resources_closed()

    def test_missing_remote_driver_has_actionable_error(self):
        self.set_response({"error": "Python package oracledb is unavailable on the jump server."}, 1)
        with self.assertRaisesRegex(RuntimeError, "oracledb.*jump server"):
            recon.fetch_sterling_orders()
        self.assert_resources_closed()

    def test_database_error_redacts_password_and_closes_resources(self):
        self.set_response({"error": "ORA-01017 test-only-password"}, 1)
        with self.assertRaisesRegex(RuntimeError, "ORA-01017") as captured:
            recon.fetch_sterling_orders()
        self.assertNotIn(recon.STERLING_DB_PASSWORD, str(captured.exception))
        self.assertIn("[REDACTED]", str(captured.exception))
        self.assert_resources_closed()

    def test_invalid_remote_output_has_actionable_error(self):
        self.stdout.read.return_value = b""
        self.stderr.read.return_value = b"python3: command not found"
        self.stdout.channel.recv_exit_status.return_value = 127
        with self.assertRaisesRegex(RuntimeError, "JUMP_PYTHON"):
            recon.fetch_sterling_orders()
        self.assert_resources_closed()

    def test_remote_timeout_closes_resources(self):
        self.stdout.read.side_effect = socket.timeout("Offline timeout")
        with self.assertRaisesRegex(RuntimeError, "Timed out.*jump server"):
            recon.fetch_sterling_orders()
        self.assert_resources_closed()

    def test_nonzero_remote_exit_is_not_accepted_as_success(self):
        self.set_response({"orders": ["000123"]}, 1)
        with self.assertRaisesRegex(RuntimeError, "status 1"):
            recon.fetch_sterling_orders()
        self.assert_resources_closed()


class RemoteQueryTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "user": "TEST_USER", "password": "test-only-password",
            "dsn": recon.STERLING_DSN, "query": recon.STERLING_RELEASE_QUERY,
            "connect_timeout": 20, "query_timeout": 900,
        }
        self.driver = types.ModuleType("oracledb")
        self.connection = MagicMock()
        self.connection.__enter__.return_value = self.connection
        self.cursor = MagicMock()
        self.cursor.__enter__.return_value = self.cursor
        self.cursor.__iter__.return_value = iter([("000123",), ("00456",), (None,)])
        self.connection.cursor.return_value = self.cursor
        self.driver.connect = Mock(return_value=self.connection)

    def execute_remote(self, driver):
        output = StringIO()
        with patch.dict(sys.modules, {"oracledb": driver}), \
             patch("sys.stdin", StringIO(json.dumps(self.settings))), redirect_stdout(output):
            with self.assertRaises(SystemExit) as captured:
                exec(compile(recon.REMOTE_QUERY_SCRIPT, "<remote-query>", "exec"),
                     {"__name__": "__main__"})
        return captured.exception.code, json.loads(output.getvalue())

    def test_query_uses_database_dsn_and_returns_json(self):
        status, result = self.execute_remote(self.driver)
        self.assertEqual(status, 0)
        self.assertEqual(result, {"orders": ["000123", "00456"]})
        self.assertEqual(self.driver.connect.call_args.kwargs["dsn"], recon.STERLING_DSN)
        self.assertEqual(self.connection.call_timeout, recon.QUERY_TIMEOUT * 1000)
        self.cursor.execute.assert_called_once_with(recon.STERLING_RELEASE_QUERY)
        self.cursor.__exit__.assert_called_once()
        self.connection.__exit__.assert_called_once()

    def test_missing_driver_is_reported_without_traceback(self):
        status, result = self.execute_remote(None)
        self.assertEqual(status, 1)
        self.assertIn("oracledb", result["error"])
        self.assertIn("JUMP_PYTHON", result["error"])

    def test_database_error_redacts_password(self):
        self.driver.connect.side_effect = RuntimeError("ORA-01017 test-only-password")
        status, result = self.execute_remote(self.driver)
        self.assertEqual(status, 1)
        self.assertNotIn(self.settings["password"], result["error"])
        self.assertIn("[REDACTED]", result["error"])


class ComparisonTests(unittest.TestCase):
    def test_missing_ids_and_duplicates_are_reported(self):
        result = recon.compare_orders(["000123", "00456", "00456"], ["000123"])
        self.assertEqual(result["missing"], ["00456"])
        self.assertEqual(result["matched_count"], 1)
        self.assertEqual(result["duplicate_wms_count"], 1)

    def test_html_escapes_missing_ids(self):
        result = recon.compare_orders(["<missing>"], [])
        report = recon.build_html_report(result, Path("test-input.csv"), "CSV")
        self.assertIn("&lt;missing&gt;", report)
        self.assertNotIn("<missing>", report)


if __name__ == "__main__":
    unittest.main()