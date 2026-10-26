from contextlib import ExitStack, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import socket
import socketserver
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import MagicMock, Mock, patch

import oracledb
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
        self.connection = MagicMock()
        self.connection.__enter__.return_value = self.connection
        self.cursor = MagicMock()
        self.cursor.__enter__.return_value = self.cursor
        self.cursor.__iter__.return_value = iter([
            (" TEST-ORDER-001 ",), ("00123",), (None,),
        ])
        self.connection.cursor.return_value = self.cursor
        self.forwarding_errors = []
        tunnel = MagicMock()
        tunnel.__enter__.return_value = (15432, self.forwarding_errors)
        self.patches.enter_context(patch.object(recon, "STERLING_DB_USER", "TEST_USER"))
        self.patches.enter_context(patch.object(recon, "STERLING_DB_PASSWORD", "test-only-password"))
        self.patches.enter_context(patch.object(recon, "KEY_PATH", __file__))
        self.patches.enter_context(patch.object(
            recon, "KNOWN_HOSTS", Path(temporary_directory) / "known_hosts"))
        self.patches.enter_context(patch.object(paramiko, "SSHClient", return_value=self.client))
        self.socket_opener = self.patches.enter_context(patch.object(
            recon, "open_jump_socket", return_value=self.connection_socket))
        self.tunnel_opener = self.patches.enter_context(patch.object(
            recon, "open_sterling_tunnel", return_value=tunnel))
        self.connector = self.patches.enter_context(patch.object(
            oracledb, "connect", return_value=self.connection))

    def assert_resources_closed(self):
        self.client.close.assert_called_once_with()
        self.connection_socket.close.assert_called_once_with()

    def test_fetch_uses_driver_and_tunnel_not_remote_commands(self):
        self.assertEqual(recon.fetch_sterling_orders(), ["TEST-ORDER-001", "00123"])
        parameters = oracledb.ConnectParams()
        parameters.parse_connect_string(recon.STERLING_DSN)
        self.tunnel_opener.assert_called_once_with(
            self.client.get_transport.return_value, parameters.host, parameters.port)
        arguments = self.connector.call_args.kwargs
        self.assertEqual(arguments["host"], "127.0.0.1")
        self.assertEqual(arguments["port"], 15432)
        self.assertEqual(arguments["service_name"], parameters.service_name)
        self.assertTrue(arguments["disable_oob"])
        self.assertIs(self.client.connect.call_args.kwargs["sock"], self.connection_socket)
        self.assertEqual(self.connection.call_timeout, recon.QUERY_TIMEOUT * 1000)
        self.client.exec_command.assert_not_called()
        self.cursor.execute.assert_called_once_with(recon.STERLING_RELEASE_QUERY.rstrip().rstrip(";"))
        self.assert_resources_closed()

    def test_authentication_failure_prevents_database_connection(self):
        self.client.connect.side_effect = paramiko.AuthenticationException("Denied")
        with self.assertRaisesRegex(RuntimeError, "SSH authentication failed"):
            recon.fetch_sterling_orders()
        self.connector.assert_not_called()
        self.tunnel_opener.assert_not_called()
        self.assert_resources_closed()

    def test_handshake_timeout_is_not_reported_as_database_failure(self):
        self.client.connect.side_effect = socket.timeout("Handshake timed out")
        with self.assertRaisesRegex(RuntimeError, "TCP connected"):
            recon.fetch_sterling_orders()
        self.connector.assert_not_called()
        self.assert_resources_closed()

    def test_forwarding_failure_has_actionable_error(self):
        self.forwarding_errors.append(paramiko.ChannelException(1, "Administratively prohibited"))
        self.connector.side_effect = oracledb.DatabaseError("Connection closed")
        with self.assertRaisesRegex(RuntimeError, "TCP forwarding permission"):
            recon.fetch_sterling_orders()
        self.assert_resources_closed()

    def test_database_error_closes_resources(self):
        self.cursor.execute.side_effect = oracledb.DatabaseError("Simulated query failure")
        with self.assertRaisesRegex(RuntimeError, "Sterling database connection or query failed"):
            recon.fetch_sterling_orders()
        self.assert_resources_closed()


class TunnelTests(unittest.TestCase):
    def test_binary_traffic_round_trip_and_cleanup(self):
        class EchoHandler(socketserver.BaseRequestHandler):
            def handle(self):
                while True:
                    data = self.request.recv(65536)
                    if not data:
                        return
                    self.request.sendall(data)

        with socketserver.ThreadingTCPServer(("127.0.0.1", 0), EchoHandler) as echo_server:
            worker = threading.Thread(target=echo_server.serve_forever,
                                      kwargs={"poll_interval": 0.1}, daemon=True)
            worker.start()
            transport = Mock()
            transport.open_channel.side_effect = lambda *args, **kwargs: socket.create_connection(
                echo_server.server_address, timeout=2)
            try:
                with recon.open_sterling_tunnel(transport, "test-database", 1521) as tunnel:
                    local_port, errors = tunnel
                    with socket.create_connection(("127.0.0.1", local_port), timeout=2) as client:
                        payload = b"CFTO tunnel probe\x00\xff"
                        client.sendall(payload)
                        received = b""
                        while len(received) < len(payload):
                            chunk = client.recv(len(payload) - len(received))
                            self.assertTrue(chunk)
                            received += chunk
                        self.assertEqual(received, payload)
                self.assertEqual(errors, [])
                self.assertEqual(transport.open_channel.call_args.args[0], "direct-tcpip")
                self.assertEqual(transport.open_channel.call_args.args[1], ("test-database", 1521))
                with self.assertRaises(OSError):
                    socket.create_connection(("127.0.0.1", local_port), timeout=0.5)
            finally:
                echo_server.shutdown()
                worker.join()

    def test_denied_forwarding_is_captured(self):
        transport = Mock()
        transport.open_channel.side_effect = paramiko.ChannelException(1, "Administratively prohibited")
        with recon.open_sterling_tunnel(transport, "test-database", 1521) as tunnel:
            local_port, errors = tunnel
            with socket.create_connection(("127.0.0.1", local_port), timeout=2) as client:
                self.assertEqual(client.recv(1), b"")
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], paramiko.ChannelException)


if __name__ == "__main__":
    unittest.main()