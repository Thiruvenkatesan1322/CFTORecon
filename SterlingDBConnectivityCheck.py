import getpass
import os
import re
import socket
import sys
import time

# Prefer cx_Oracle (used by the recon scripts); python-oracledb works without an Oracle Client.
try:
    import cx_Oracle as oracle
except ImportError:
    import oracledb as oracle

# Sterling reporting DB: the JDBC thin URL without the "jdbc:oracle:thin:@" prefix.
STERLING_DSN = ("(DESCRIPTION =(ADDRESS_LIST =(FAILOVER = ON)(LOAD_BALANCE = OFF)"
                "(ADDRESS = (PROTOCOL = TCP)(HOST = YOUR_DB_HOST)(PORT = 1521))"
                "(ADDRESS = (PROTOCOL = TCP)(HOST = YOUR_DB_HOST)(PORT = 1521)))"
                "(CONNECT_DATA =(SERVICE_NAME = YOUR_DB_SERVICE)))")

# Oracle drivers reject a trailing ";".
STERLING_RELEASE_QUERY = """select distinct trim(ms.key2) from STRADMIN1.ms_msg_replay ms, STRADMIN1.SPKV_YFS_ORDER_HEADER YOH, STRADMIN1.SPKV_YFS_ORDER_LINE YOL where
YOH.ORDER_HEADER_KEY=YOL.ORDER_HEADER_KEY AND trim(ms.key2)=trim(YOL.EXTN_FP_RELEASE_ID) AND to_char(ms.createts, 'DD-MON-YYYY') = to_char(sysdate-1, 'DD-MON-YYYY') and ms.Key2 <> '0' and
ms.msg_type_destination = 'DN_FOM' and YOH.ORDER_TYPE IN('CFTO_UK','CFTO_IE') and yoh.enterprise_key = 'MS_UK' and yoh.order_closed in ('N')  and yol.ordered_qty > '0'"""

SAMPLE_ROWS = 10


def main():
    print("[1/3] Network check")
    for host, port in re.findall(r"HOST\s*=\s*([^)\s]+)\)\s*\(PORT\s*=\s*(\d+)", STERLING_DSN, re.IGNORECASE):
        started = time.time()
        try:
            socket.create_connection((host, int(port)), timeout=5).close()
            print("  {}:{} reachable ({:.0f} ms)".format(host, port, (time.time() - started) * 1000))
        except OSError as error:
            print("  {}:{} NOT reachable: {}".format(host, port, error))

    user = os.environ.get("STERLING_DB_USER") or input("Sterling DB user: ")
    password = os.environ.get("STERLING_DB_PASSWORD") or getpass.getpass("Sterling DB password: ")

    print("[2/3] Login to YOUR_DB_SERVICE as {}".format(user))
    try:
        connection = oracle.connect(user=user, password=password, dsn=STERLING_DSN)
    except oracle.DatabaseError as error:
        print("  FAILED: {}".format(error))
        return 1

    try:
        cursor = connection.cursor()
        cursor.execute("select sys_context('USERENV', 'INSTANCE_NAME'), sys_context('USERENV', 'SERVER_HOST') from dual")
        instance, server = cursor.fetchone()
        print("  OK: Oracle {}, instance {} on {}".format(connection.version, instance, server))

        print("[3/3] SterlingReleaseQuery (yesterday's CFTO releases)")
        started = time.time()
        cursor.execute(STERLING_RELEASE_QUERY)
        rows = cursor.fetchall()
        print("  OK: {} rows in {:.1f} s".format(len(rows), time.time() - started))
        for row in rows[:SAMPLE_ROWS]:
            print("    {}".format(row[0]))
    except oracle.DatabaseError as error:
        print("  FAILED: {}".format(error))
        return 1
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
