# Data source connections

This describes the implemented development candidate. Automated tests use isolated
files and fake database/Desktop connections. They do not qualify an authenticated
source, a tenant, a gateway, or the packaged Windows application.

## Supported routes

| Source | Studio route | Requirements and limits |
| --- | --- | --- |
| CSV, Excel XLS/XLSX/XLSM, Parquet | Choose local files, review, import | Signature-based detection; immutable original and normalized snapshots; macros are not executed |
| SQL Server / Azure SQL | Data → Sources → Database connections | Named ODBC DSN, importable `pyodbc`, Microsoft ODBC Driver 17/18 of the application's architecture, valid TLS certificate and read access |
| Snowflake | Data → Sources → Database connections | Named Python connector profile, account/warehouse/database configuration, supported authentication and read access |
| Other Power BI Desktop connectors | Configure and load the source in Desktop, save a PBIX, then Open Power BI report | Desktop owns the connector, credentials, source permissions, transformations, drivers and network access |
| Remote semantic models / thin reports | Conditional native Desktop query route | A queryable engine for the exact working-copy report must be available; importing a layout alone does not establish a remote model connection |

Studio does not implement every connector in Microsoft's catalog. In particular,
there is no direct Studio SharePoint, PostgreSQL, Oracle, SAP, Dataverse, Fabric,
cloud semantic-model or XMLA connector. These belong to the Desktop-managed route
when that connector and connection mode work in the installed Desktop environment.
Check Microsoft's [Desktop data sources](https://learn.microsoft.com/en-us/power-bi/connect-data/desktop-data-sources)
and [Power Query connector catalog](https://learn.microsoft.com/en-us/power-query/connectors/)
for host availability and connector-specific requirements. Import, DirectQuery,
Dual, Direct Lake and live connections require separate qualification.
See [model storage modes](model-storage-modes.md) for table-level classification,
snapshot coverage and native calculation behavior.

## Direct database setup and repeat imports

Use **Connect to a database** on first launch to create a workspace without
selecting a local folder. The source form separates prerequisites from successful
authentication. A loaded SDK or available ODBC driver is not proof of a working
profile, source access or sign-in.

For SQL Server/Azure SQL, install a supported
[Microsoft ODBC driver](https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server)
and configure the named DSN for the app's architecture. Studio requests encrypted
connections with certificate validation. Do not work around a certificate failure
by disabling validation. For Snowflake, use the Python connector's named
[connection profile](https://docs.snowflake.com/en/developer-guide/python-connector/python-connector-connect).
Passwords are not entered in Studio or stored in snapshot metadata. Protect vendor
configuration and use a source account with the required read permissions.

Choose the schema, browse tables, optionally preview, then review and import.
Studio issues bounded SELECT statements; it accepts no user or model SQL. It
imports up to ten tables with a configurable per-table limit of 1–1,000,000 rows,
defaulting to 100,000. It also enforces time, memory, disk, column and cell bounds.
An over-limit or failed extraction does not activate a partial snapshot; staged
Parquet files are removed and a sanitized receipt remains.

**Import again** restores the saved provider, profile, schema, tables and row
limit for review. It performs no source request until the user explicitly imports.
Failed imports retain their exact idempotency envelope in the open form. If import
succeeds but screen loading fails, **Retry reload** fetches the result without
importing again. Changing settings clears the previous selection and retry state.

The original bytes used for profiling and normalization are the same retained
copy. Text identifiers retain leading zeroes; nullable large integers retain their
digits and browser previews serialize integers outside JavaScript's safe range
as decimal text. Wide integers and decimal values that cannot round-trip through
the local numeric representation remain text, with an explanatory finding.
Timezone-aware database timestamps normalize to UTC with a finding; naive
timestamps retain their wall-clock values. Mixed aware/naive values fail with an
actionable message rather than assuming a timezone.

## Power BI-managed sources and refresh

1. In Desktop, use Get data, configure the source and authenticate. Verify its
   tables, transformations and connection mode, then load or refresh successfully.
2. Save the PBIX and open it in Studio. Import reads the saved report and any
   embedded rows; it does not run Power Query or authenticate to the source.
3. Native queries use the exact report's working copy and model connection. DAX
   Run can work without a local snapshot when that Desktop engine is available.
   AI formula generation and local evaluations still require saved rows.

Keep these operations distinct:

| Operation | What changes |
| --- | --- |
| Refresh in Desktop | Contacts configured sources and refreshes the Desktop model |
| Save PBIX, then Studio Read data again | Reads the saved model's embedded rows into a new local snapshot |
| Refresh a Studio native visual/query | Queries the existing Desktop model; this alone does not refresh its source |
| Import again for a direct Studio database source | Queries selected source tables into a new immutable snapshot |

Generated-project hydration binds a named localhost model connection to the
verified Desktop process and rejects missing, ambiguous or changed engine
identity. It cannot fall back to a different open report. However, Microsoft
lists Desktop model processing through external tools as unsupported; this
automation requires explicit version-specific acceptance evidence before release.
See [external tools](https://learn.microsoft.com/en-us/power-bi/transform-model/desktop-external-tools).

Power BI service refresh is a separate deployment concern. Credentials, gateways,
source network access, permissions and licenses must be configured there. See
[refresh](https://learn.microsoft.com/en-us/power-bi/connect-data/refresh-data) and
[gateway data sources](https://learn.microsoft.com/en-us/power-bi/connect-data/service-gateway-data-sources).
Studio is not a universal gateway or credential manager.

## Qualification

Run `npm run test:data-browser` from `apps/desktop` for fixture-only source UI,
first-launch and native DAX regressions. The release runner's `-Browser` gate
includes this suite. Backend source tests cover immutable ingestion, exact values,
cleanup, request replay and native report isolation.

For every source advertised at release, record an actual successful connection,
preview, import, restart, explicit repeat import and changed-source result using
approved credentials. Exercise expired sign-in, denied permissions, network and
TLS failures, large/null/mixed types, empty tables and row limits. For each
Power BI-managed source/mode, compare saved rows and native DAX with Desktop and
qualify refresh, reconnect and two-open-report isolation. Repeat on the installed
application: SDK build flags do not prove the packaged SDK loads, and ODBC drivers
are external prerequisites.

The October 4 development host loads the Snowflake and pyodbc SDKs, but lacks
Microsoft ODBC Driver 17/18. No authenticated database or tenant connection was
used in this audit. Prerequisite detection and automated isolation checks are
verified; real-account and installer qualification remain open.
