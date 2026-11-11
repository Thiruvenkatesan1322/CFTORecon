"""Run the Sterling count query through an SSH jump server and print its result.

Configure the connection variables below before running.
Requires paramiko locally, and Python 3 with oracledb on the jump server.
The query uses the supplied fixed date bounds, both exclusive.
"""

import json
from pathlib import Path
import shlex
import sys


STERLING_DB_USER = ""
STERLING_DB_PASSWORD = ""
STERLING_DSN = (
    "(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=YOUR_DB_HOST)(PORT=1521))"
    "(CONNECT_DATA=(SERVICE_NAME=YOUR_DB_SERVICE)))"
)

JUMP_HOST = "YOUR_JUMP_HOST"
JUMP_PORT = 22
JUMP_USER = "YOUR_JUMP_USER"
KEY_PATH = "PATH_TO_SSH_PRIVATE_KEY"
JUMP_PYTHON = "python3"
KNOWN_HOSTS = Path(__file__).resolve().parent / "known_hosts"
SSH_CONNECT_TIMEOUT = 20
QUERY_TIMEOUT = 900

QUERY = """SELECT count(distinct yoh.EXTN_channel_ORDER_id)
FROM STRADMIN1.SPKV_yfs_order_header yoh
WHERE yoh.order_header_key > '20261111'
  and yoh.order_header_key < '2026111110'"""

REMOTE_QUERY_SCRIPT = """
import json
import sys

settings = {}
stage = "preparing jump-server Python"
try:
    settings = json.load(sys.stdin)
    import oracledb

    stage = "connecting to the database"
    with oracledb.connect(
        user=settings["user"], password=settings["password"], dsn=settings["dsn"],
        tcp_connect_timeout=settings["connect_timeout"], retry_count=0, disable_oob=True,
    ) as connection:
        connection.call_timeout = settings["query_timeout"] * 1000
        stage = "running the count query"
        with connection.cursor() as cursor:
            cursor.execute(settings["query"])
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("The count query returned no result row.")
            count = int(row[0])
    print(json.dumps({"count": count}))
except Exception as error:
    message = str(error)
    password = settings.get("password", "")
    if password:
        message = message.replace(password, "[REDACTED]")
    print(json.dumps({"error": "Failed while " + stage + ": " + message}))
    sys.exit(1)
"""


def fetch_order_count():
    if not STERLING_DB_USER.strip() or not STERLING_DB_PASSWORD:
        raise ValueError("Set STERLING_DB_USER and STERLING_DB_PASSWORD before running.")
    if not Path(KEY_PATH).is_file():
        raise ValueError("SSH private key not found. Set KEY_PATH for this computer.")

    import paramiko

    with paramiko.SSHClient() as client:
        client.load_system_host_keys()
        KNOWN_HOSTS.touch(exist_ok=True)
        client.load_host_keys(str(KNOWN_HOSTS))
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=JUMP_HOST, port=JUMP_PORT, username=JUMP_USER,
            key_filename=KEY_PATH, look_for_keys=False, allow_agent=False,
            timeout=SSH_CONNECT_TIMEOUT, auth_timeout=20, banner_timeout=20,
        )
        transport = client.get_transport()
        if transport is None or not transport.is_active():
            raise RuntimeError("The SSH connection closed before the query started.")
        transport.set_keepalive(30)

        command = "{} -c {}".format(shlex.quote(JUMP_PYTHON), shlex.quote(REMOTE_QUERY_SCRIPT))
        stdin, stdout, stderr = client.exec_command(
            command, timeout=QUERY_TIMEOUT + SSH_CONNECT_TIMEOUT)
        stdin.write(json.dumps({
            "user": STERLING_DB_USER,
            "password": STERLING_DB_PASSWORD,
            "dsn": STERLING_DSN,
            "query": QUERY,
            "connect_timeout": SSH_CONNECT_TIMEOUT,
            "query_timeout": QUERY_TIMEOUT,
        }))
        stdin.flush()
        stdin.channel.shutdown_write()
        output = stdout.read().decode("utf-8", errors="replace")
        error_output = stderr.read().decode("utf-8", errors="replace")
        status = stdout.channel.recv_exit_status()

    try:
        result = json.loads(output)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "No valid result from the jump server. Check JUMP_PYTHON and remote oracledb. "
            + error_output.strip()) from error
    if not isinstance(result, dict):
        raise RuntimeError("The jump server returned an invalid query result.")
    if status != 0 or result.get("error"):
        raise RuntimeError(result.get("error") or "Remote Python exited with status {}.".format(status))
    count = result.get("count")
    if type(count) is not int or count < 0:
        raise RuntimeError("The jump server did not return a valid non-negative count.")
    return count


def main():
    try:
        count = fetch_order_count()
        print("Distinct channel order count: {}".format(count))
        return 0
    except Exception as error:
        message = str(error)
        if STERLING_DB_PASSWORD:
            message = message.replace(STERLING_DB_PASSWORD, "[REDACTED]")
        print("FAILED: {}".format(message), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())