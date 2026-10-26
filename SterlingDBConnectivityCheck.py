import csv
import getpass
import os
import re
import sys
import time

import paramiko

# Path: this host -> SSH -> YOUR_JUMP_HOST -> port 1521 -> Sterling DB. The query runs in sqlplus on the jump host.
JUMP_HOST = "YOUR_JUMP_HOST"
JUMP_USER = "YOUR_JUMP_USER"
KEY_PATH  = r"PATH_TO_SSH_PRIVATE_KEY"

SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
KNOWN_HOSTS   = os.path.join(SCRIPT_DIR, "known_hosts")
OUTPUT_CSV    = os.path.join(SCRIPT_DIR, "SterlingReleaseExtract.csv")
QUERY_TIMEOUT = 900

# Sterling reporting DB: the JDBC thin URL without the "jdbc:oracle:thin:@" prefix.
# .23 is tried first (the host given for YOUR_DB_SERVICE); .22 is the fallback.
STERLING_DSN = ("(DESCRIPTION =(ADDRESS_LIST =(FAILOVER = ON)(LOAD_BALANCE = OFF)"
                "(ADDRESS = (PROTOCOL = TCP)(HOST = YOUR_DB_HOST)(PORT = 1521))"
                "(ADDRESS = (PROTOCOL = TCP)(HOST = YOUR_DB_HOST)(PORT = 1521)))"
                "(CONNECT_DATA =(SERVICE_NAME = YOUR_DB_SERVICE)))")

# No trailing ";" here; SQLPLUS_SCRIPT adds it.
STERLING_RELEASE_QUERY = """select distinct trim(ms.key2) from STRADMIN1.ms_msg_replay ms, STRADMIN1.SPKV_YFS_ORDER_HEADER YOH, STRADMIN1.SPKV_YFS_ORDER_LINE YOL where
YOH.ORDER_HEADER_KEY=YOL.ORDER_HEADER_KEY AND trim(ms.key2)=trim(YOL.EXTN_FP_RELEASE_ID) AND to_char(ms.createts, 'DD-MON-YYYY') = to_char(sysdate-1, 'DD-MON-YYYY') and ms.Key2 <> '0' and
ms.msg_type_destination = 'DN_FOM' and YOH.ORDER_TYPE IN('CFTO_UK','CFTO_IE') and yoh.enterprise_key = 'MS_UK' and yoh.order_closed in ('N')  and yol.ordered_qty > '0'"""

SAMPLE_ROWS = 10

# Sent on stdin so the DB password never shows in the jump host's process list.
SQLPLUS_SCRIPT = """WHENEVER SQLERROR EXIT FAILURE
SET DEFINE OFF
SET PAGESIZE 0 FEEDBACK OFF HEADING OFF VERIFY OFF TRIMOUT ON TAB OFF LINESIZE 32767
CONNECT {user}/"{password}"@{dsn}
PROMPT __ROWS_START__
{query};
PROMPT __ROWS_END__
EXIT
"""


def run(client, command, stdin_text=None, timeout=30):
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    if stdin_text is not None:
        stdin.write(stdin_text)
        stdin.flush()
        stdin.channel.shutdown_write()
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    return stdout.channel.recv_exit_status(), out, err


def main():
    print("[1/4] SSH to {} as {}".format(JUMP_HOST, JUMP_USER))
    client = paramiko.SSHClient()
    open(KNOWN_HOSTS, "a").close()
    client.load_host_keys(KNOWN_HOSTS)
    # Trust on first use: the key is saved to KNOWN_HOSTS and a changed key is rejected afterwards.
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(hostname=JUMP_HOST, username=JUMP_USER, key_filename=KEY_PATH,
                       look_for_keys=False, allow_agent=False, timeout=20)
        print("  OK: logged in to {}".format(run(client, "hostname")[1].strip()))

        print("[2/4] Port 1521 from the jump host")
        for host, port in re.findall(r"HOST\s*=\s*([^)\s]+)\)\s*\(PORT\s*=\s*(\d+)", STERLING_DSN, re.IGNORECASE):
            reachable = run(client, "timeout 5 bash -c '</dev/tcp/{}/{}'".format(host, port))[0] == 0
            try:
                client.get_transport().open_channel("direct-tcpip", (host, int(port)), ("127.0.0.1", 0), timeout=10).close()
                tunnel = "allowed"
            except paramiko.SSHException as error:
                tunnel = "not available ({})".format(error)
            print("  {}:{} {} | SSH tunnel {}".format(host, port, "reachable" if reachable else "NOT reachable", tunnel))

        print("[3/4] sqlplus on the jump host")
        status, out, err = run(client, "bash -lc 'command -v sqlplus && sqlplus -V'")
        if status != 0:
            print("  FAILED: sqlplus not found or not runnable. {}".format((out + err).strip()))
            return 1
        print("  OK: " + " | ".join(line.strip() for line in out.splitlines() if line.strip()))

        user = os.environ.get("STERLING_DB_USER") or input("Sterling DB user: ")
        password = os.environ.get("STERLING_DB_PASSWORD") or getpass.getpass("Sterling DB password: ")

        print("[4/4] SterlingReleaseQuery via sqlplus as {}".format(user))
        script = SQLPLUS_SCRIPT.format(user=user, password=password, dsn=re.sub(r"\s+", "", STERLING_DSN),
                                       query=STERLING_RELEASE_QUERY)
        started = time.time()
        status, out, err = run(client, "bash -lc 'sqlplus -s /nolog'", stdin_text=script, timeout=QUERY_TIMEOUT)
        lines = [line.strip() for line in out.splitlines()]
        if status != 0 or "__ROWS_START__" not in lines or "__ROWS_END__" not in lines:
            errors = [line.strip() for line in (out + err).splitlines() if re.match(r"\s*(ORA-|SP2-|ERROR)", line)]
            print("  FAILED: {}".format(" | ".join(errors) or "sqlplus exit status {}".format(status)))
            return 1
        rows = [line for line in lines[lines.index("__ROWS_START__") + 1:lines.index("__ROWS_END__")] if line]
        with open(OUTPUT_CSV, "w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["FP_RELEASE_ID"])
            writer.writerows([row] for row in rows)
        print("  OK: {} rows in {:.1f} s, saved to {}".format(len(rows), time.time() - started, OUTPUT_CSV))
        for row in rows[:SAMPLE_ROWS]:
            print("    " + row)
        return 0
    except (paramiko.SSHException, OSError) as error:
        print("  FAILED: {}".format(error))
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
