# svcparity

Put an ArcGIS Server service's pool, instance, maxRecordCount and extension settings back after an overwrite publish, and say so when it cannot.

Here is the failure, with made-up numbers. A map service has been tuned over months. It runs on
dedicated instances, up to six per node. Its FeatureServer extension answers queries only. Its
maxRecordCount is 4000, because a web app pages by that number. Somebody republishes the project
with Overwrite. The publish succeeds, the item keeps its URL, and the map draws.

The next day the service is on the shared pool with default instance counts. The app's paging
loop gets a quarter of the rows it expects, because maxRecordCount is 1000 again. The extension
lists Create and Update on a layer that was read-only. Every log is green. Nothing failed, so
nothing alerted, and the first report is a person asking why the map is slow.

On services that I run, an overwrite has reset the pool, the instance counts, maxRecordCount and
the FeatureServer extension. I could not find an Esri page that says which service settings an
overwrite carries over, so this tool does not assume any. It saves the settings before the
overwrite and compares them afterwards.

## The alternatives

- **Export Site.** Esri's [Export Site](https://developers.arcgis.com/rest/enterprise-administration/server/exportsite/)
  operation exports the whole site configuration: service configurations and properties, users and
  roles, the machine list and more. That is more than this tool sees, and it is the right tool for a
  disaster recovery backup. It works on the whole site. I did not exercise Import Site, so I make
  no claim about restoring one service from an export.
- **By hand.** The [Edit Service](https://developers.arcgis.com/rest/enterprise-administration/server/editservice/)
  page describes the manual route: read the service JSON, change it, and post the complete JSON
  back. This tool automates that route for the settings above, and it adds the checks below.
- **Other open source.** [arcgis_service_manager](https://github.com/dannykrouk/arcgis_service_manager)
  (Apache-2.0) describes itself as saving service states, stopping services and restoring from a
  saved state. I read its README and not its code, so I cannot say whether it covers pool,
  instance or extension settings. [restapi](https://github.com/Bolton-and-Menk-GIS/restapi)
  is a Python library for the REST API, and it can be a base for tools like this one.

On 2026-10-13 I searched GitHub for the name `svcparity` and for ArcGIS Server admin, backup and
restore terms. I found no repository of that name. The closest ones are listed above.

```
$ python svcparity.py --self-test
svcparity self-test: no arcpy, no real server, a stub on 127.0.0.1
--------------------------------------------------------------------
...
PASS  the state holds the allowlisted keys and not the service definition path  <-- pinned defect
...
PASS  the plan never touches the definition path the overwrite installed  <-- pinned defect
PASS  the edit body keeps every key the plan did not name, because the server drops a key that is left out  <-- pinned defect
...
PASS  restoring a service to LESS edit access needs no permission  <-- pinned defect
...
PASS  a unique prefix --ap of --apply is REFUSED by the parser and writes nothing  <-- pinned defect
...
PASS  a 200 with an error body is an error, which a status check misses  <-- pinned defect
...
PASS  a rejected login exits 3 and the password the server echoed back is masked  <-- pinned defect
...
PASS  a snapshot with nothing in it compares nothing and exits 4, not a clean 0  <-- pinned defect
...
PASS  a restore that would turn edit capabilities on is refused, writes nothing and leaves no backup  <-- pinned defect
...
PASS  the overwrite itself is kept: the new definition path survives, and so does every key the tool does not own  <-- pinned defect
...
PASS  a service whose JSON carries a credential-like key is never edited, because the edit call would resend a masked value  <-- pinned defect
...
PASS  no token, password or extension credential appears in anything any run above printed or wrote
--------------------------------------------------------------------
125 assertions, 0 failed
```

## Requirements

Python 3.9 or later. The standard library only. No `arcpy`, no Pro licence, no third-party package.

```
git clone https://github.com/uhsear/svcparity.git
python svcparity.py --self-test
```

The self-test runs a stub of the Administrator API on `127.0.0.1`. It needs no server and no
credentials.

## Usage

Take the snapshot before the overwrite. The tool writes it to standard output, so redirect it.

```
python svcparity.py --snapshot --server https://gis.example.com/arcgis \
    --service Demo/Roads.MapServer --service Demo/Zones.MapServer > before.json
```

Overwrite the services. Then compare. Nothing is written.

```
python svcparity.py --diff --state before.json --server https://gis.example.com/arcgis
```

```
Demo/Roads.MapServer: 8 differences from the snapshot
  ext.FeatureServer.capabilities                 live Query,Create,Update, saved Query
  ext.FeatureServer.properties.maxRecordCount    live 1000, saved 4000
  pool.maxIdleTime                               live 1800, saved 900
  pool.maxInstancesPerNode                       live 2, saved 6
  pool.minInstancesPerNode                       live 1, saved 2
  properties.maxRecordCount                      live 1000, saved 4000
  properties.schemaLockingEnabled                live true, saved false
  provider                                       live DMaps, saved ArcObjects11
VERDICT: DIFFERS
```

Plan the restore, which still writes nothing. Then apply it with a backup file.

```
python svcparity.py --restore --state before.json --server https://gis.example.com/arcgis
python svcparity.py --restore --state before.json --server https://gis.example.com/arcgis \
    --apply --backup undo.json
```

```
Demo/Roads.MapServer: 8 differences from the snapshot
  ...
backup written: undo.json
Demo/Roads.MapServer: restored and read back
VERDICT: MATCH
```

| Flag | Mode | What it does |
|---|---|---|
| `--snapshot` | mode | Print the settings of each `--service` as JSON. |
| `--diff` | mode | Compare the live services with `--state`. Read-only. |
| `--restore` | mode | Plan the restore. With `--apply`, perform it. |
| `--self-test` | mode | Run the offline assertions and exit. |
| `--server URL` | all | The ArcGIS Server URL, such as `https://host/arcgis`. The tool adds `/admin`. |
| `--service NAME` | snapshot | `Folder/Name.Type`, such as `Demo/Roads.MapServer`. Repeat for more. |
| `--state FILE` | diff, restore | A snapshot file. |
| `--portal URL` | all | The portal URL, only for the read-only sharing diff. |
| `--item ID` | snapshot | A portal item whose sharing is recorded. Repeat for more. |
| `--apply` | restore | Perform the restore. Without it, nothing is written. |
| `--backup FILE` | restore | Where the pre-restore state goes. Required with `--apply`. The file must not exist. |
| `--allow-edit-capabilities` | restore | Allow a restore that turns edit capabilities on. |

Credentials come from the environment and never from a flag.

| Variable | Use |
|---|---|
| `SVCPARITY_TOKEN` | A token you already hold. |
| `SVCPARITY_USER`, `SVCPARITY_PASSWORD` | Mint a token from the server's `generateToken`. |
| `SVCPARITY_PORTAL_TOKEN` | The portal token for `--portal`. It defaults to `SVCPARITY_TOKEN`. |

On a federated site, Esri says the Administrator Directory takes one of two things
([Accessing the Administrator Directory on a federated server](https://enterprise.arcgis.com/en/portal/11.2/administer/linux/accessing-the-administrator-directory-on-a-federated-server.htm)).
One is "a portal token generated for a portal administrator or publisher". The other is "the
primary site administrator name and password". Unless you use the primary site administrator, get a portal token yourself and pass it in
`SVCPARITY_TOKEN`. The `generateToken` call that `SVCPARITY_USER` triggers uses
`client=requestip`, one of the values in Esri's
[Generate Token](https://developers.arcgis.com/rest/services-reference/enterprise/generate-token.htm)
reference.

Exit codes:

| Code | Verdict | Meaning |
|---|---|---|
| 0 | `MATCH` | Every saved setting matches, or was restored and read back. |
| 1 | `DIFFERS` | The live service differs and was not fixed: a plan, a sharing difference, or a change the tool cannot make. |
| 2 | `FAILED` | An apply failed. The service was put back, or the output says it was not. |
| 3 | `UNREADABLE` | A server, service or item could not be read. |
| 4 | `UNVERIFIED` | The snapshot held nothing to compare. |
| 5 | `REFUSED` | A guard refused the apply before it wrote anything. |
| 64 | | Usage error. |
| 70 | | A fault in this tool, not a finding about the service. |

## What it saves

| Group | Keys |
|---|---|
| Provider | `provider`, restored through `changeProvider` |
| Pool and instances | `minInstancesPerNode`, `maxInstancesPerNode`, `maxWaitTime`, `maxStartupTime`, `maxIdleTime`, `maxUsageTime`, `recycleInterval`, `loadBalancing`, `isolationLevel` |
| Service properties | `maxRecordCount`, `maxBufferCount`, `maxImageHeight`, `maxImageWidth`, `maxDomainCodeCount`, `antialiasingMode`, `textAntialiasingMode`, `schemaLockingEnabled` |
| Each extension | `enabled`, `capabilities` and its `properties` |

The lists are the `CONFIGURATION` block at the top of the file. The pool keys and the `extensions`
layout are the ones on Esri's Edit Service page. Esri's
[Setting map service properties](https://enterprise.arcgis.com/en/server/10.7/publish-services/linux/setting-map-service-properties.htm)
page describes the map service settings. It does not list the JSON key names, so I took those from
service JSON that I have read.

The tool does not save the rest of the service JSON, and this is on purpose. The `properties`
object also holds the path of the service definition that the overwrite just installed. Posting
that back would undo the overwrite. The tool saves an allowlist and nothing else.

The tool also leaves out an extension property whose name looks like a credential or a location.
The name contains `pass`, `pwd`, `secret`, `cred`, `token`, `api key`, `dir`, `path`, `folder`,
`url` or `uri`. The snapshot command names each one on standard error. The value never reaches a
snapshot or a backup.

## Sharing is a read-only diff

`--item` and `--portal` record an item's `access` level (`private`, `shared`, `org` or `public`)
and the groups that the token can see. `--diff` and `--restore` report an access change and the
groups lost or gained. Nothing in this version writes to the portal.

Esri's Groups operation says "Only shows the groups that the calling user can access"
([Groups](https://developers.arcgis.com/rest/users-groups-and-items/groups/)). A token with less
visibility than the snapshot's token reports groups as lost. Use the same account for both.

The tool does not look up an item from a service. I found no documented Administrator API key
that links the two, so you name each item with `--item`.

## What keeps it safe

- **Only `--apply` writes.** Without it, `--snapshot`, `--diff` and `--restore` make GET requests
  and print. The one exception is the `generateToken` POST that mints a token from a user and
  password. The self-test pins that a restore plan sends no POST.
- **The parser refuses a prefix of `--apply`.** The parser sets `allow_abbrev=False`. The
  self-test pins that `--ap` and `--appl` exit 64 and write nothing.
- **`--apply` needs `--backup`.** The tool writes the pre-restore state to a file that must not
  exist, before its first write. It makes no file when no service needs a change.
- **A failed restore puts the service back.** After each restore the tool waits for the service
  to report its earlier state. Then it reads the service again and compares. A rejected edit, a
  service that does not return, or a setting that does not read back sends the service to its
  pre-restore state. If that also fails, the output says "NOT put back" and the exit code is 2.
- **It will not turn edit capabilities on by surprise.** A restore can switch on Create, Update,
  Delete, Editing or Sync where they are off now. The tool then refuses with exit 5.
  `--allow-edit-capabilities` lets it proceed. Restoring a service to less edit access needs no
  permission.
- **It never edits a service whose JSON has a credential-like key.** The edit call resends the
  whole JSON. The tool cannot tell a masked value from a real one, so it refuses with exit 5.
  Change those services in Manager.
- **Plain `http://` is accepted for `127.0.0.1` and `localhost` only.** Any other server needs `https://`. There
  is no flag to turn certificate checks off.
- **The token moves the way Esri says.** A GET carries the token in the query. A POST carries it
  in the body, as the Administrator API pages require. The password goes in a POST body only.
- **Every credential is masked in the output.** The tool replaces any token or password it has
  seen with `***`. That includes one that a server echoes back in an error message.

## Why the obvious version is wrong

The obvious tool saves the whole service JSON before the overwrite and posts it back afterwards.
That tool puts the old service definition path back, and the overwrite is undone.

The next obvious tool posts only the keys that changed. The Edit Service page says "you need to
submit the complete JSON representation of the service". It also says "Any other properties that
are left out of the JSON are not persisted by the server." A partial post deletes everything it
omits.
This tool reads the live JSON, changes the keys that differ, and posts the whole document. The
self-test stub keeps only what a request carries, so a partial post fails an assertion.

The edit call is not documented to change the provider. This tool changes it first, through
[Change Provider](https://developers.arcgis.com/rest/enterprise-administration/server/change-provider/),
reads the service again, and then edits. Change Provider accepts `ArcObjects11` (dedicated) and
`DMaps` (shared). The tool reports any other saved provider as not restorable.

Editing a service restarts it. The Edit Service page says so. The tool waits for the service to
reach its earlier state, using the
[Status](https://developers.arcgis.com/rest/enterprise-administration/server/service-status/)
resource. A service that was stopped stays stopped, and the wait does not time out.

## Where each claim was exercised

Every row ran against the local stub, through `main()`, with the output checked.

| Claim | Where |
|---|---|
| An overwritten service is diffed and restored, and a new snapshot equals the old one | The restore assertions in `--self-test` |
| The overwrite's definition path survives a restore | `the overwrite itself is kept` |
| A prefix of `--apply` is refused and writes nothing | `--ap` and `--appl` assertions |
| A 200 reply with an error body is an error | `a 200 with an error body is an error` |
| A snapshot that compares nothing exits 4 | `compares nothing and exits 4` |
| Edit capabilities need `--allow-edit-capabilities` | The two `edit capabilities` assertions |
| A rejected edit, a refused provider, a missing read-back and a service that never returns all end in a revert or a clear failure | The `attempt` assertions |
| No credential reaches any output | The last assertion greps everything every run printed or wrote |
| A real subprocess run, with the stub on loopback, matches the sample output above | I ran `--snapshot`, `--diff`, `--restore` and `--restore --apply --backup` as separate processes and kept the output |

## Limits

- **No real ArcGIS Server.** I could not reach an ArcGIS Server from the machine that built this.
  The stub follows the Esri pages linked above. Those pages show only the success reply of Edit
  Service, and the Status page does not list every `realTimeState` value. The tool treats a reply
  with an `error` object, or with `status` set to `error`, as a failure, and the stub uses the same
  shapes. I have not seen them from a live server in this build. Run `--diff` and a plan on a test
  service first.
- **Tokens in the query string.** A GET carries its token in the query, so a proxy or an access
  log can see it. Esri
  [recommends](https://enterprise.arcgis.com/en/server/11.5/administer/linux/accessing-arcgis-token-secured-web-services.htm)
  the `X-Esri-Authorization` header for GET requests to token-secured services. I found no
  statement that the Administrator API accepts it, so I did not use it. Ask for a short token.
- **Service types.** Change Provider does not support SDS, geocode or geoprocessing services
  ([Change Provider](https://developers.arcgis.com/rest/enterprise-administration/server/change-provider/)).
  The server's error is reported and the service is put back.
- **A missing extension is not created.** The live service may lack a saved extension. The tool
  reports it as not restorable and exits 1.
- **No cross-site mode.** The tool compares a service with its own earlier snapshot. This version
  does not compare one site with another.
- **Downtime.** Each restore restarts the service.
- **TLS.** The tool fails on a certificate that your machine does not trust. Fix the trust store.
- **Coverage.** Branch coverage on Windows with Python 3.13 is 100 percent (1013 of 1013 statements, 266 of 266 branches). No line is excluded
  and no line is unreachable offline. I did not install `coverage` on the Linux host, so I did not
  measure coverage there. Both hosts run the same assertions.
- **Self-test counts.** The self-test prints 125 assertions on each of three runs: Windows with Python 3.13, Windows with Python 3.9 and Linux with Python 3.12. The Windows and Linux outputs are byte-identical, so no difference needs explaining.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [svcguard](https://github.com/uhsear/svcguard) - stop and start services around risky work, and guarantee the restart
- [svcdrift](https://github.com/uhsear/svcdrift) - diff a published feature service against the data it was published from
- [svcsource](https://github.com/uhsear/svcsource) - report the database and dataset behind every service on a site
- [sharewatch](https://github.com/uhsear/sharewatch) - diff an organization's sharing posture, read-only
- [restfake](https://github.com/uhsear/restfake) - a mock ArcGIS REST server that fails the way ArcGIS fails
