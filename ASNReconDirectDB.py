from CreateIncident import create_incident
import cx_Oracle
import pandas as pd
import logging
import shutil
import sys
import os

# Direct-DB version of ASNReconMain.py: queries WMS and Sterling without the bastion host.

REMEDY_URL          = "YOUR_REMEDY_URL"
REMEDY_USER         = "YOUR_REMEDY_USER"
REMEDY_PASSWORD_ENV = "ASN_REMEDY_PASSWORD"
SUMMARY             = "Daily ASN Recon between OMS and WMS"
ASSIGNEE_TEAM       = "Sterling Support"

WMS_DB = {
    "name": "WMS",
    "host": "YOUR_DB_HOST",
    "port": 1521,
    "service": "YOUR_DB_SERVICE",
    "user": "YOUR_DB_USER",
    "password_env": "ASN_WMS_DB_PASSWORD",
}

# TODO: fill in the Sterling OMS database details (read-only account).
STERLING_DB = {
    "name": "Sterling",
    "host": "",
    "port": 1521,
    "service": "",
    "user": "",
    "password_env": "ASN_STERLING_DB_PASSWORD",
}

BASE_DIR               = r"PATH_TO_RECON_OUTPUT"
ATTACHMENT_DIR         = os.path.join(BASE_DIR, "AttachmentFolder")
LOG_PATH               = os.path.join(BASE_DIR, "Process Logs.log")
WMS_EXTRACT            = os.path.join(BASE_DIR, "WMSExtract.csv")
STERLING_EXTRACT       = os.path.join(BASE_DIR, "SterlingExtract.csv")
EXCEPTION_EXTRACT      = os.path.join(BASE_DIR, "ExceptionOutput.csv")
MISMATCH_FILE          = os.path.join(ATTACHMENT_DIR, "ASNMismatch.csv")
WITH_EXCEPTION_FILE    = os.path.join(ATTACHMENT_DIR, "OrdersWithException.csv")
WITHOUT_EXCEPTION_FILE = os.path.join(ATTACHMENT_DIR, "OrdersWithoutException.csv")

RED   = "#ff0000"
AMBER = "#FFBF00"
GREEN = "#00cc00"

# cx_Oracle rejects SQL text ending in ";".
WMS_QUERY = """SELECT
oh.order_id order_id,
CASE
WHEN SUM(ol.qty_ordered) = SUM(ol.qty_shipped) THEN
'COMPLETELY SHIPPED'
WHEN SUM(ol.qty_ordered) > SUM(ol.qty_shipped) THEN
'PARTIALLY SHIPPED'
END shipment_status
FROM
order_header oh
INNER JOIN order_line ol ON 1 = 1
AND oh.order_id = ol.order_id
AND oh.order_type IN (
'E-COM',
'DEMANDWARE'
)
AND ( TO_CHAR(oh.shipped_date, 'yyyy/mm/dd hh24:mi:ss') >= TO_CHAR(SYSDATE - 1, 'yyyy/mm/dd')
|| ' 03:30:00' )
AND ( TO_CHAR(oh.shipped_date, 'yyyy/mm/dd hh24:mi:ss') <= TO_CHAR(SYSDATE, 'yyyy/mm/dd')
|| ' 03:30:00' )
GROUP BY
oh.order_id
"""

STERLING_QUERY = """select distinct yor.extn_fp_release_id as FP_RELEASE_ID
from STRADMIN1.yfs_order_release yor, STRADMIN1.yfs_order_release_status yors
where yor.order_release_key=yors.order_release_key
and order_release_status_key > concat(to_char(sysdate-1,'YYYYMMDD'),'03')
and order_release_status_key < concat(to_char(sysdate,'YYYYMMDD'),'04')
and yor.shipnode_key='4624'
and yors.status in ('3700.001','1400')"""

EXCEPTION_QUERY = """SELECT DISTINCT TRIM(OL.EXTN_FP_RELEASE_ID) AS ORDER_ID, TRIM(OL.REQ_DELIVERY_DATE) AS DELIVERY_DATE, TRIM(YS.STATUS_NAME) AS STATUS
FROM STRADMIN1.SPKV_YFS_ORDER_HEADER OH, STRADMIN1.SPKV_YFS_ORDER_LINE OL, STRADMIN1.YFS_ORDER_RELEASE_STATUS ORS, STRADMIN1.YFS_STATUS YS
WHERE OH.ORDER_HEADER_KEY=OL.ORDER_HEADER_KEY
AND OL.ORDER_LINE_KEY=ORS.ORDER_LINE_KEY
AND TRIM(YS.STATUS)=TRIM(ORS.STATUS)
AND OH.DOCUMENT_TYPE='0001'
AND OH.ENTERPRISE_KEY='MS_UK'
AND OH.ORDER_CLOSED IN ('N')
AND OH.NEXT_ALERT_TS IS NOT NULL
AND ORS.STATUS_QUANTITY > 0
AND YS.PROCESS_TYPE_KEY='ORDER_FULFILLMENT'
AND OL.SHIPNODE_KEY='4624'
and ys.status_name='Awaiting Despatch'
AND OL.EXTN_FP_RELEASE_ID IN (
    SELECT DISTINCT trim(xmltype(message).extract('//Shipment/@FPReleaseID').getStringVal())
    FROM STRADMIN1.SPKV_YFS_REPROCESS_ERROR
    WHERE FLOW_NAME IN ('ReadSplitShipASNMessageFromFPService','ReadASNMessageFromFPService')
    AND trunc(createts) > trunc(SYSDATE-70)
    AND STATE IN ('Initial'))"""

CSS = """<style>
.UrlTable {
  width:50%;
  border: 0.7mm solid #111111;
  border-collapse:collapse;
  height: 50%;
  bottom: 50px;
}
.UrlTable td{
  padding:5px 5px;
  font-size :16px;
  font-family: calibri;
  border: 0.5mm solid #000000;
  text-align:center; color:black;
}
.UrlTable th{
  padding:5px 5px;
  border:2px solid #000000;
  background-color: #80ccff;
  color: black;
  text-align :  center;
  font-family: calibri;
  font-size : 18px;
}
</style>
"""

EXCEPTION_TABLE = """
<style>
#table2 {
font-family: calibri;
border-collapse: collapse;
width: 100%;
}
#table2 td, #table2 th {
border: 1px solid #ddd;
padding: 8px;
}
#table2 th {
padding-top: 12px;
padding-bottom: 12px;
text-align: center;
background-color: #46BFF6;
}
</style>
<br><br>
<h4>Exception Table:</h4>
<table id='table2'>
<thead><tr><th>Scenario</th><th>Comment</th><th>Incident</th></tr></thead>"""

INCIDENT_ROW = "<tr><td>{}</td><td>{}</td><td>{}</td></tr>"
MAIL_INTRO   = "Hi all, <br><br>Please find the <b>ASN Recon</b> status below,<br><br>"

logger = logging.getLogger()


def fail(message):
    print("Exception occurred in usecase!" + "<b>{}</b>".format(message))
    sys.exit()


def env(name):
    value = os.environ.get(name)
    if not value:
        raise KeyError("Environment variable {} is not set".format(name))
    return value


def fetch(db, query):
    if not (db["host"] and db["service"] and db["user"]):
        raise ValueError("{} DB connection details are not configured".format(db["name"]))
    dsn = cx_Oracle.makedsn(db["host"], db["port"], service_name=db["service"])
    connection = cx_Oracle.connect(user=db["user"], password=env(db["password_env"]), dsn=dsn)
    try:
        cursor = connection.cursor()
        cursor.execute(query)
        columns = [column[0] for column in cursor.description]
        data = pd.DataFrame(cursor.fetchall(), columns=columns)
    finally:
        connection.close()
    for column in data.columns:
        data[column] = data[column].fillna("").astype(str).str.strip()
    logger.info("Fetched {} rows from the {} DB".format(len(data), db["name"]))
    return data


def rag(count, amber_limit):
    if count == 0:
        return GREEN
    return AMBER if count < amber_limit else RED


def summary_table(rows):
    html = ("<div><table class='UrlTable' style='width: 650px;'><tbody><tr>"
            "<th style='width: 300px;'>Category</th><th style='width: 39px;'>Count</th>"
            "<th style='width: 70px;'>RAG Status</th></tr>")
    for category, count, colour in rows:
        rag_cell = "<td bgcolor='{}'></td>".format(colour) if colour else "<td></td>"
        html += "<tr><td>{}</td><td>{}</td>{}</tr>".format(category, count, rag_cell)
    return html + "</tbody></table></div>"


def raise_incident(orders, path, scenario, label):
    try:
        orders.to_csv(path, index=False)
    except Exception:
        logger.exception("Error writing the {} to {}".format(label, path))
        return INCIDENT_ROW.format(scenario, "There was an issue in extracting the {}.".format(label), "N/A")
    try:
        incident = create_incident(REMEDY_URL, REMEDY_USER, env(REMEDY_PASSWORD_ENV),
                                   orders.to_string(index=False), SUMMARY, ASSIGNEE_TEAM)
    except Exception:
        logger.exception("Error raising the incident for the {}".format(label))
        return INCIDENT_ROW.format(scenario, "Issue in raising incident to Sterling Support. PFA, file containing the {}.".format(label), "N/A")
    logger.info("Incident {} assigned to Sterling Support for the {}".format(incident, label))
    return INCIDENT_ROW.format(scenario, "Incident containing the {} has been assigned to Sterling Support".format(label), incident)


def main():
    logging.basicConfig(filename=LOG_PATH, format="%(asctime)s : %(levelname)s : %(message)s",
                        filemode="w", level=logging.INFO)

    try:
        for name in os.listdir(ATTACHMENT_DIR):
            path = os.path.join(ATTACHMENT_DIR, name)
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)
    except Exception:
        logger.exception("Error while deleting the older attachments")
        fail("There was an error while deleting the older attachments. Kindly perform the activity manually for today.")

    try:
        wms = fetch(WMS_DB, WMS_QUERY)
        wms.to_csv(WMS_EXTRACT, index=False)
    except Exception:
        logger.exception("Error while fetching the WMS extract")
        fail("There was an error while fetching the WMS data from the database. Kindly perform the activity manually for today.")

    try:
        sterling = fetch(STERLING_DB, STERLING_QUERY)
        sterling.to_csv(STERLING_EXTRACT, index=False)
    except Exception:
        logger.exception("Error while fetching the Sterling extract")
        fail("There was an error while fetching the Sterling data from the database. Kindly perform the activity manually for today.")

    try:
        mismatch = wms.loc[~wms["ORDER_ID"].isin(sterling["FP_RELEASE_ID"]), ["ORDER_ID"]]
        if len(mismatch):
            mismatch.to_csv(MISMATCH_FILE, index=False)
        logger.info("Recon completed: {} WMS ASNs, {} Sterling releases, {} missing in Sterling".format(
            len(wms), len(sterling), len(mismatch)))
    except Exception:
        logger.exception("Error while performing the ASN recon")
        fail("There was an error while performing the reconciliation between the Sterling and WMS data. Kindly perform the activity manually for today.")

    if len(mismatch) == 0:
        print(CSS + MAIL_INTRO + summary_table([
            ("Total number of ASN triggered from WMS", len(wms), None),
            ("Total number of ASN missing", "No ASN mismatch today", GREEN),
            ("Number of ASN in Sterling Exception queue", "No orders", GREEN),
            ("Number of ASN missing in Sterling", "No orders", GREEN),
        ]))
        return

    rows = [
        ("Total number of ASN triggered from WMS", len(wms), None),
        ("Total number of ASN missing", len(mismatch), rag(len(mismatch), 50)),
    ]

    try:
        exceptions = fetch(STERLING_DB, EXCEPTION_QUERY)
        exceptions.to_csv(EXCEPTION_EXTRACT, index=False)
        with_exception = exceptions.merge(mismatch, on="ORDER_ID", how="inner").rename(columns={"ORDER_ID": "FP_RELEASE_ID"})
        without_exception = mismatch[~mismatch["ORDER_ID"].isin(exceptions["ORDER_ID"])]
    except Exception:
        logger.exception("Error while fetching the Sterling exception data")
        print("Exception occurred in usecase!" + CSS + MAIL_INTRO + summary_table(rows)
              + "<br><br>There was an error while fetching the Exception data from the Sterling database. "
              "Please find the attached ASN missing orders. <br><br> <b>Sterling Team,</b><br> Kindly check the "
              "mismatched orders in Sterling exception queue manually for today and intimate the WMS team on the "
              "status of those orders.")
        sys.exit()

    rows += [
        ("Number of ASN in Sterling Exception queue", len(with_exception), rag(len(with_exception), 30)),
        ("Number of ASN missing in Sterling", len(without_exception), rag(len(without_exception), 20)),
    ]

    incidents = ""
    if len(with_exception):
        incidents += raise_incident(with_exception, WITH_EXCEPTION_FILE, "ASN in exception queue",
                                    "orders in the Sterling exception queue")
    if len(without_exception):
        incidents += raise_incident(without_exception, WITHOUT_EXCEPTION_FILE, "ASN not in exception queue",
                                    "orders not in the Sterling exception queue")

    exception_table = EXCEPTION_TABLE + incidents + "</table>" if incidents else ""
    print("ASN Mismatch exists!", CSS + MAIL_INTRO + summary_table(rows) + exception_table)


if __name__ == "__main__":
    main()
