#!/usr/bin/env python
"""Put an ArcGIS Server service's pool, instance, maxRecordCount and extension settings back after an overwrite.

Overwriting a service republishes it from a new service definition. The service
comes back running, the map draws, and the settings somebody tuned on the live
service (dedicated or shared instances, the instance counts, maxRecordCount, the
FeatureServer extension) are whatever the new publish carried. Nothing reports
the difference, because nothing failed.

Take a snapshot BEFORE the overwrite. Afterwards, --diff lists what moved and
--restore puts it back, one service at a time, with a backup and a read-back.

    python svcparity.py --self-test
    python svcparity.py --snapshot --server https://gis.example.com/arcgis \\
        --service Demo/Roads.MapServer > before.json
    python svcparity.py --diff --state before.json --server https://gis.example.com/arcgis
    python svcparity.py --restore --state before.json --server https://gis.example.com/arcgis
    python svcparity.py --restore --state before.json --server https://gis.example.com/arcgis \\
        --apply --backup undo.json

Sharing is a read-only diff in this version: --item and --portal record an
item's access level and groups, and --diff reports a change. Nothing here
writes to the portal.

Credentials come from the environment, never from a flag: SVCPARITY_TOKEN, or
SVCPARITY_USER with SVCPARITY_PASSWORD, and SVCPARITY_PORTAL_TOKEN for the portal.

Exit codes: 0 identical or restored, 1 differences remain, 2 an apply failed,
3 a server could not be read, 4 nothing was compared, 5 refused, 64 usage
error, 70 a fault in this tool.
"""

from __future__ import print_function

import argparse
import collections
import contextlib
import copy
import importlib.util
import io
import json
import os
import re
import sys
import time
import traceback
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# Service JSON keys at the top level that this tool saves and restores. These are
# the pool and instance settings that Esri documents on the Edit Service page.
POOL_KEYS = (
    "minInstancesPerNode", "maxInstancesPerNode", "maxWaitTime",
    "maxStartupTime", "maxIdleTime", "maxUsageTime", "recycleInterval",
    "loadBalancing", "isolationLevel",
)

# Keys inside the service "properties" object that this tool saves and restores.
# An allowlist on purpose: the same object holds the path of the service
# definition the overwrite just installed, and putting that back undoes the
# overwrite itself.
PROP_KEYS = (
    "maxRecordCount", "maxBufferCount", "maxImageHeight", "maxImageWidth",
    "maxDomainCodeCount", "antialiasingMode", "textAntialiasingMode",
    "schemaLockingEnabled",
)

# Extension fields saved and restored for every extension, besides its
# "properties" object.
EXT_KEYS = ("enabled", "capabilities")

# A key whose name matches this looks like a credential. The Edit Service call
# must resend the whole service JSON, and this tool cannot tell a masked value
# from a real one, so a service that carries such a key is never edited.
SECRET_KEY = re.compile("pass|pwd|secret|cred|token|api.?key", re.I)

# An extension property whose name matches this is never read into a snapshot or
# a backup: it is a credential or a location, and a location is a thing the
# overwrite is allowed to have changed.
WITHHOLD = re.compile(SECRET_KEY.pattern + "|dir|path|folder|url|uri", re.I)

# Extension capabilities that let a client change data. Restoring one of these
# onto a service that did not expose it needs --allow-edit-capabilities.
EDIT_CAPS = ("create", "update", "delete", "editing", "sync")

# The only values the changeProvider operation accepts: dedicated, then shared.
PROVIDERS = ("ArcObjects11", "DMaps")

HTTP_TIMEOUT = 60       # seconds for one request
TOKEN_MINUTES = 15      # lifetime asked for when a token is minted here
START_WAIT = 120        # seconds to wait for a service to return to its state
POLL_SECONDS = 2        # pause between status reads

# =============================================================================
# End of CONFIGURATION.
# =============================================================================

FORMAT = "svcparity/1"
SERVICE_RE = re.compile(r"^[A-Za-z0-9_\-]+(/[A-Za-z0-9_\-]+)*\.[A-Za-z]+$")
ITEM_RE = re.compile(r"^[0-9a-fA-F]{32}$")
LOOPBACK = ("127.0.0.1", "localhost")

EXIT_OK, EXIT_DIFF, EXIT_FAILED, EXIT_READ, EXIT_NONE, EXIT_REFUSED = range(6)
EXIT_USAGE, EXIT_INTERNAL = 64, 70
VERDICTS = {0: "MATCH", 1: "DIFFERS", 2: "FAILED", 3: "UNREADABLE",
            4: "UNVERIFIED", 5: "REFUSED"}

Change = collections.namedtuple("Change", "path live want how note")
Row = collections.namedtuple("Row", "svc live changes error")


class UsageError(Exception):
    """The command line or a file named on it is wrong. Exit 64."""


class ServiceError(Exception):
    """A server answered wrongly, or did not answer. Exit 3 or 2."""


class Timer(object):
    """The clock and the sleep, in one place so the self-test can replace them."""

    def __init__(self):
        self.now = time.time
        self.sleep = time.sleep


TIMER = Timer()
SECRETS = []


def say(text="", err=False):
    """Print ASCII only, with every credential this run has seen masked."""
    for secret in SECRETS:
        text = text.replace(secret, "***")
    text = text.encode("ascii", "replace").decode("ascii")
    print(text, file=sys.stderr if err else sys.stdout)


# ------------------------------------------------------------------ pure core

def norm(value):
    """The server returns booleans and numbers as strings, or as themselves."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def caps_key(value):
    """Capabilities as a set, so a reordered list is not a difference."""
    return frozenset(t.strip().lower() for t in norm(value).split(",")
                     if t.strip())


def same(path, live, want):
    if live is None:
        return False
    if path[0] == "ext" and len(path) == 3 and path[2] == "capabilities":
        return caps_key(live) == caps_key(want)
    return norm(live) == norm(want)


def extract_state(adm):
    """The settings this tool owns, from one service's admin JSON.

    Returns (state, withheld). Only allowlisted keys are read, so nothing else
    in the service JSON can reach a snapshot or a backup.
    """
    props = adm.get("properties", {})
    exts, withheld = {}, []
    for ext in adm.get("extensions", []):
        keep = {}
        for key, value in ext.get("properties", {}).items():
            if WITHHOLD.search(key):
                withheld.append("ext.%s.properties.%s" % (ext["typeName"], key))
            else:
                keep[key] = value
        entry = dict((k, ext.get(k)) for k in EXT_KEYS)
        entry["properties"] = keep
        exts[ext["typeName"]] = entry
    state = {
        "provider": adm.get("provider"),
        "pool": dict((k, adm[k]) for k in POOL_KEYS if k in adm),
        "properties": dict((k, props[k]) for k in PROP_KEYS if k in props),
        "extensions": exts,
    }
    return state, withheld


def _pairs(state):
    yield ("provider",), state.get("provider")
    for group in ("pool", "properties"):
        for key, value in state.get(group, {}).items():
            yield (group, key), value
    for name, ext in state.get("extensions", {}).items():
        for key in EXT_KEYS:
            yield ("ext", name, key), ext.get(key)
        for key, value in ext.get("properties", {}).items():
            yield ("ext", name, "properties", key), value


def flatten(state):
    """One path (a tuple) per saved setting. No value means no setting."""
    return dict((p, v) for p, v in _pairs(state) if v is not None)


def plan(saved, live_adm):
    """What differs between a saved state and a live service, as Change rows.

    how is "provider" (needs changeProvider), "edit" (goes into the edit
    call) or "stuck" (this tool cannot make that change; note says why).
    """
    current = flatten(extract_state(live_adm)[0])
    present = set(e["typeName"] for e in live_adm.get("extensions", []))
    out = []
    for path, want in sorted(flatten(saved).items()):
        live = current.get(path)
        if same(path, live, want):
            continue
        how, note = "edit", ""
        if path == ("provider",):
            how = "provider"
            if want not in PROVIDERS:
                how, note = "stuck", "changeProvider accepts only %s" % (
                    " or ".join(PROVIDERS))
        elif path[0] == "ext" and path[1] not in present:
            how, note = "stuck", "the live service has no such extension"
        out.append(Change(path, live, want, how, note))
    return out


def _child(parent, key):
    if not isinstance(parent.get(key), dict):
        parent[key] = {}
    return parent[key]


def apply_changes(live_adm, changes):
    """The full service JSON with the "edit" changes written in.

    The Edit Service operation persists only what the request carries, so the
    whole current JSON goes back, not just the keys that differ.
    """
    adm = copy.deepcopy(live_adm)
    for change in changes:
        if change.how != "edit":
            continue
        path = change.path
        if path[0] == "pool":
            adm[path[1]] = change.want
        elif path[0] == "properties":
            _child(adm, "properties")[path[1]] = change.want
        else:
            ext = [e for e in adm["extensions"] if e["typeName"] == path[1]][0]
            if len(path) == 4:
                _child(ext, "properties")[path[3]] = change.want
            else:
                ext[path[2]] = change.want
    return adm


def secret_keys(node, path=""):
    """Paths of every credential-like key in a piece of service JSON."""
    out = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = path + "/" + key
            if SECRET_KEY.search(key):
                out.append(here)
            out += secret_keys(value, here)
    elif isinstance(node, list):
        for value in node:
            out += secret_keys(value, path)
    return out


def _active_edit_caps(adm):
    out = set()
    for ext in adm.get("extensions", []):
        if norm(ext.get("enabled")).lower() == "true":
            for cap in caps_key(ext.get("capabilities")):
                if cap in EDIT_CAPS:
                    out.add("%s:%s" % (ext["typeName"], cap))
    return out


def edit_exposure(live_adm, new_adm):
    """Edit capabilities that would be on after the edit and are off now."""
    return sorted(_active_edit_caps(new_adm) - _active_edit_caps(live_adm))


def diff_sharing(saved, live):
    """Differences in item access and visible groups. Read-only, never restored.

    live maps an item id to its state, or to None when it could not be read.
    """
    out = []
    for item_id, want in sorted(saved.items()):
        now = live.get(item_id)
        if now is None:
            out.append("item %s: could not be read" % item_id)
            continue
        if now["access"] != want["access"]:
            out.append("item %s: access is %s, saved %s"
                       % (item_id, now["access"], want["access"]))
        lost = sorted(set(want["groups"]) - set(now["groups"]))
        gained = sorted(set(now["groups"]) - set(want["groups"]))
        if lost or gained:
            out.append("item %s: groups lost [%s], gained [%s]"
                       % (item_id, ",".join(lost), ",".join(gained)))
    return out


def decide(failed, unreadable, refused, compared, remaining):
    """The exit status. The worst thing that happened wins."""
    if failed:
        return EXIT_FAILED
    if unreadable:
        return EXIT_READ
    if refused:
        return EXIT_REFUSED
    if not compared:
        return EXIT_NONE
    if remaining:
        return EXIT_DIFF
    return EXIT_OK


def show(value):
    return "<absent>" if value is None else norm(value)


def render(row):
    """The report lines for one service."""
    if row.error:
        return ["%s: UNREADABLE: %s" % (row.svc, row.error)]
    if not row.changes:
        return ["%s: matches the snapshot" % row.svc]
    lines = ["%s: %d differences from the snapshot" % (row.svc, len(row.changes))]
    for c in row.changes:
        tail = "" if c.how != "stuck" else "   [NOT RESTORABLE: %s]" % c.note
        lines.append("  %-46s live %s, saved %s%s"
                     % (".".join(c.path), show(c.live), show(c.want), tail))
    return lines


# --------------------------------------------------------------------- state

def valid_state(st):
    if not isinstance(st, dict):
        return False
    for group in ("pool", "properties", "extensions"):
        if not isinstance(st.get(group, {}), dict):
            return False
    for ext in st.get("extensions", {}).values():
        if not isinstance(ext, dict) or not isinstance(ext.get("properties", {}), dict):
            return False
    return True


def valid_item(item):
    return (isinstance(item, dict) and isinstance(item.get("access"), str)
            and isinstance(item.get("groups"), list)
            and all(isinstance(g, str) for g in item["groups"]))


def check_state_doc(doc, where):
    """Validate a snapshot document, and refuse a service or item name that is
    not shaped like one, because the name becomes part of a request path."""
    ok = (isinstance(doc, dict) and doc.get("format") == FORMAT
          and isinstance(doc.get("services"), dict)
          and isinstance(doc.get("items"), dict))
    if ok:
        ok = (all(SERVICE_RE.match(k) and valid_state(v)
                  for k, v in doc["services"].items())
              and all(ITEM_RE.match(k) and valid_item(v)
                      for k, v in doc["items"].items()))
    if not ok:
        raise UsageError("%s is not a svcparity snapshot" % where)
    return doc


def load_state(path):
    try:
        with io.open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        raise UsageError("cannot read %s: %s" % (path, exc))
    return check_state_doc(doc, path)


# ------------------------------------------------------------------- network

def check_url(url):
    """Plain http is accepted for loopback only, so a credential never crosses
    a network in clear text."""
    parts = urlsplit(url)
    loopback = parts.hostname in LOOPBACK
    if not parts.hostname or parts.scheme not in ("http", "https") \
            or (parts.scheme == "http" and not loopback):
        raise UsageError("%s: use an https:// URL (http:// only for 127.0.0.1)"
                         % url)
    return url.rstrip("/")


def opener_for(url):
    """Never send loopback traffic through a proxy."""
    if urlsplit(url).hostname in LOOPBACK:
        return build_opener(ProxyHandler({}))
    return build_opener()


def reply_message(doc):
    err = doc.get("error")
    if isinstance(err, dict):
        parts = [err.get("message")] + list(err.get("details") or [])
    else:
        parts = list(doc.get("messages") or [])
    return "; ".join(str(p) for p in parts if p) or "no message"


def http_json(method, url, fields):
    """One request. The token travels in the query for GET and in the body for
    POST. An error is a ServiceError that never contains the query string."""
    body = None
    if method == "GET":
        full = url + "?" + urlencode(fields)
    else:
        full, body = url, urlencode(fields).encode("ascii")
    try:
        with opener_for(url).open(Request(full, data=body), timeout=HTTP_TIMEOUT) as r:
            doc = json.loads(r.read().decode("utf-8"))
    except OSError as exc:
        raise ServiceError("%s %s failed: %s" % (method, url, exc))
    except ValueError:
        raise ServiceError("%s %s did not return JSON" % (method, url))
    if not isinstance(doc, dict):
        raise ServiceError("%s %s did not return a JSON object" % (method, url))
    if "error" in doc or doc.get("status") == "error":
        raise ServiceError("%s %s: the server reported an error: %s"
                           % (method, url, reply_message(doc)))
    return doc


def is_service(doc):
    return (isinstance(doc.get("serviceName"), str)
            and isinstance(doc.get("properties", {}), dict)
            and isinstance(doc.get("extensions", []), list)
            and all(isinstance(e, dict) and isinstance(e.get("typeName"), str)
                    and isinstance(e.get("properties", {}), dict)
                    for e in doc.get("extensions", [])))


class Client(object):
    """The Administrator API calls this tool makes, and the portal reads."""

    def __init__(self, server, portal, token, portal_token):
        self.admin = server + "/admin"
        self.portal = portal + "/sharing/rest" if portal else None
        self.token, self.portal_token = token, portal_token

    def _post(self, path, **fields):
        fields.update(f="json", token=self.token)
        doc = http_json("POST", self.admin + path, fields)
        if doc.get("status") != "success":
            raise ServiceError("POST %s%s did not report success"
                               % (self.admin, path))
        return doc

    def get_service(self, svc):
        doc = http_json("GET", "%s/services/%s" % (self.admin, svc),
                        {"f": "json", "token": self.token})
        if not is_service(doc):
            raise ServiceError("%s: the reply is not a service description" % svc)
        return doc

    def status(self, svc):
        return http_json("GET", "%s/services/%s/status" % (self.admin, svc),
                         {"f": "json", "token": self.token})

    def edit(self, svc, adm):
        self._post("/services/%s/edit" % svc, service=json.dumps(adm))

    def change_provider(self, svc, provider):
        self._post("/services/%s/changeProvider" % svc, provider=provider)

    def get_item(self, item_id):
        base = "%s/content/items/%s" % (self.portal, item_id)
        fields = {"f": "json", "token": self.portal_token}
        info = http_json("GET", base, fields)
        groups = http_json("GET", base + "/groups", fields)
        if not isinstance(info.get("access"), str):
            raise ServiceError("item %s: the reply is not an item" % item_id)
        ids = set(g["id"] for k in ("admin", "member", "other")
                  for g in groups.get(k, []))
        return {"access": info["access"], "groups": sorted(ids)}


def mint_token(server, user, password):
    SECRETS.append(password)
    doc = http_json("POST", server + "/admin/generateToken", {
        "username": user, "password": password, "client": "requestip",
        "expiration": TOKEN_MINUTES, "f": "json"})
    if not isinstance(doc.get("token"), str):
        raise ServiceError("generateToken returned no token")
    SECRETS.append(doc["token"])
    return doc["token"]


def connect(args):
    server = check_url(args.server)
    token = os.environ.get("SVCPARITY_TOKEN")
    user = os.environ.get("SVCPARITY_USER")
    password = os.environ.get("SVCPARITY_PASSWORD")
    if token:
        SECRETS.append(token)
    elif user and password:
        token = mint_token(server, user, password)
    else:
        raise UsageError("set SVCPARITY_TOKEN, or SVCPARITY_USER and "
                         "SVCPARITY_PASSWORD")
    portal_token = os.environ.get("SVCPARITY_PORTAL_TOKEN") or token
    SECRETS.append(portal_token)
    portal = check_url(args.portal) if args.portal else None
    return Client(server, portal, token, portal_token)


def wait_state(client, svc, target):
    """Wait until the service reports the state it had before the write."""
    end = TIMER.now() + START_WAIT
    while True:
        seen = client.status(svc).get("realTimeState")
        if seen == target:
            return
        if TIMER.now() >= end:
            raise ServiceError("%s: still %s after %d seconds, wanted %s"
                               % (svc, seen, START_WAIT, target))
        TIMER.sleep(POLL_SECONDS)


def converge(client, svc, want):
    """Make the live service match want. Returns the changes still left.

    The provider goes first through changeProvider, because the edit call is not
    documented to change it. A change this tool cannot make is not "left": it is
    reported by plan() and skipped here.
    """
    target = client.status(svc).get("configuredState")
    live = client.get_service(svc)
    changes = plan(want, live)
    for change in [c for c in changes if c.how == "provider"]:
        client.change_provider(svc, change.want)
        wait_state(client, svc, target)
        live = client.get_service(svc)
        changes = plan(want, live)
    edits = [c for c in changes if c.how == "edit"]
    if edits:
        client.edit(svc, apply_changes(live, edits))
        wait_state(client, svc, target)
    return [c for c in plan(want, client.get_service(svc)) if c.how != "stuck"]


# ------------------------------------------------------------------ commands

def read_items(client, ids):
    out = {}
    for item_id in ids:
        try:
            out[item_id] = client.get_item(item_id)
        except ServiceError as exc:
            say("%s" % exc, err=True)
            out[item_id] = None
    return out


def cmd_snapshot(args, client):
    services, withheld = {}, {}
    for svc in args.service:
        services[svc], gone = extract_state(client.get_service(svc))
        if gone:
            withheld[svc] = gone
    items = {}
    for item_id in args.item or []:
        items[item_id] = client.get_item(item_id)
    doc = {"format": FORMAT, "services": services, "items": items,
           "withheld": withheld}
    say(json.dumps(doc, indent=1, sort_keys=True))
    for svc, names in sorted(withheld.items()):
        say("svcparity: %s: left out of the snapshot: %s"
            % (svc, ", ".join(names)), err=True)
    return EXIT_OK


def survey(state, client):
    rows = []
    for svc, want in sorted(state["services"].items()):
        try:
            live = client.get_service(svc)
            rows.append(Row(svc, live, plan(want, live), None))
        except ServiceError as exc:
            rows.append(Row(svc, None, [], str(exc)))
    return rows


def report(rows, sharing):
    for row in rows:
        for line in render(row):
            say(line)
    for line in sharing:
        say("sharing (read-only, never restored): " + line)


def cmd_diff_restore(args, client, state, restore):
    rows = survey(state, client)
    live_items = read_items(client, sorted(state["items"]))
    sharing = diff_sharing(state["items"], live_items)
    report(rows, sharing)
    unreadable = any(r.error for r in rows) or None in live_items.values()
    compared = sum(len(flatten(state["services"][r.svc]))
                   for r in rows if not r.error) + len(state["items"])
    pending = [r for r in rows if r.changes]
    failed = refused = False
    left = len(sharing) + sum(len(r.changes) for r in pending)
    if restore and args.apply and pending:
        refused, failed, left = apply_restore(args, client, state, pending,
                                              len(sharing))
    elif restore and pending:
        say("Plan only. Nothing was written. Add --apply --backup FILE to "
            "restore.")
    verdict = decide(failed, unreadable, refused, compared, left)
    say("VERDICT: %s" % VERDICTS[verdict])
    return verdict


def apply_restore(args, client, state, pending, sharing_left):
    """Plan every service first, then write. Returns (refused, failed, left)."""
    todo = [r for r in pending if any(c.how != "stuck" for c in r.changes)]
    stuck_left = sum(1 for r in pending for c in r.changes if c.how == "stuck")
    problems = []
    for row in todo:
        edits = [c for c in row.changes if c.how == "edit"]
        keys = secret_keys(row.live)
        exposure = edit_exposure(row.live, apply_changes(row.live, edits))
        if keys:
            problems.append(
                "%s carries credential-like keys (%s). The edit call resends "
                "the whole JSON, and a masked value would replace the real one."
                % (row.svc, ", ".join(keys)))
        if exposure and not args.allow_edit_capabilities:
            problems.append(
                "%s would turn on edit capabilities that are off now: %s. "
                "Pass --allow-edit-capabilities if that is intended."
                % (row.svc, ", ".join(exposure)))
    for problem in problems:
        say("REFUSED: " + problem, err=True)
    if problems:
        return True, False, len(pending)
    backup = {"format": FORMAT, "items": {}, "withheld": {},
              "services": dict((r.svc, extract_state(r.live)[0]) for r in todo)}
    if todo:
        write_backup(args.backup, backup)
    failed, left = False, stuck_left + sharing_left
    for row in todo:
        outcome = restore_one(client, row.svc, state["services"][row.svc],
                              backup["services"][row.svc])
        failed = failed or outcome is not None
        left += 0 if outcome is None else 1
    return False, failed, left


def write_backup(path, doc):
    try:
        with io.open(path, "x", encoding="ascii") as fh:
            fh.write(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    except OSError as exc:
        raise UsageError("cannot create the backup %s: %s. It must not exist "
                         "yet." % (path, exc))
    say("backup written: %s" % path)


def restore_one(client, svc, want, before):
    """Restore one service. Returns None on success, else the failure text.

    A failed edit, a service that does not come back, or a setting that does
    not read back puts the service in its pre-restore state again.
    """
    try:
        left = converge(client, svc, want)
        problem = None if not left else "did not read back: " + ", ".join(
            ".".join(c.path) for c in left)
    except ServiceError as exc:
        problem = str(exc)
    if problem is None:
        say("%s: restored and read back" % svc)
        return None
    try:
        undone = not converge(client, svc, before)
    except ServiceError as exc:
        say("%s: revert failed: %s" % (svc, exc), err=True)
        undone = False
    say("%s: FAILED: %s. %s" % (svc, problem, "Put back as found." if undone
                                else "NOT put back: use the backup file."),
        err=True)
    return problem


def run(args):
    if args.snapshot:
        if bool(args.item) != bool(args.portal):
            raise UsageError("--item and --portal go together")
        for svc in args.service:
            if not SERVICE_RE.match(svc):
                raise UsageError("%s is not Folder/Name.Type" % svc)
        for item_id in args.item or []:
            if not ITEM_RE.match(item_id):
                raise UsageError("%s is not a 32 character item id" % item_id)
        return cmd_snapshot(args, connect(args))
    state = load_state(args.state)
    if bool(state["items"]) != bool(args.portal):
        raise UsageError("--portal goes with a snapshot that holds items, and "
                         "only with that")
    return cmd_diff_restore(args, connect(args), state, args.restore)


# ---------------------------------------------------------------- command line

ALLOWED = {
    "snapshot": ("server", "service", "portal", "item"),
    "diff": ("server", "state", "portal"),
    "restore": ("server", "state", "portal", "apply", "backup",
                "allow_edit_capabilities"),
    "self_test": (),
}
REQUIRED = {"snapshot": ("server", "service"), "diff": ("server", "state"),
            "restore": ("server", "state"), "self_test": ()}
OPTIONS = ("server", "service", "state", "portal", "item", "apply", "backup",
           "allow_edit_capabilities")


def build_parser():
    ap = argparse.ArgumentParser(
        prog="svcparity.py", allow_abbrev=False,
        description="Put an ArcGIS Server service's pool, instance, "
                    "maxRecordCount and extension settings back after an "
                    "overwrite.",
        epilog="Nothing is written without --apply. Credentials come from "
               "SVCPARITY_TOKEN, or SVCPARITY_USER and SVCPARITY_PASSWORD.")
    modes = ap.add_mutually_exclusive_group(required=True)
    modes.add_argument("--snapshot", action="store_true",
                       help="print the settings of --service as JSON")
    modes.add_argument("--diff", action="store_true",
                       help="compare the live services with --state")
    modes.add_argument("--restore", action="store_true",
                       help="plan, or with --apply perform, the restore")
    modes.add_argument("--self-test", dest="self_test", action="store_true",
                       help="run the offline assertions and exit")
    ap.add_argument("--server", help="ArcGIS Server URL, e.g. "
                    "https://host/arcgis. The /admin part is added here.")
    ap.add_argument("--service", action="append",
                    help="Folder/Name.Type, e.g. Demo/Roads.MapServer; repeat")
    ap.add_argument("--state", help="a snapshot file from --snapshot")
    ap.add_argument("--portal", help="portal URL, e.g. https://host/portal; "
                    "only for the read-only sharing diff")
    ap.add_argument("--item", action="append",
                    help="a portal item id whose sharing is recorded; repeat")
    ap.add_argument("--apply", action="store_true",
                    help="perform the restore. Without it nothing is written.")
    ap.add_argument("--backup", help="file for the pre-restore state; it must "
                    "not exist. Required with --apply.")
    ap.add_argument("--allow-edit-capabilities", dest="allow_edit_capabilities",
                    action="store_true",
                    help="allow a restore that turns edit capabilities on")
    return ap


def validate(args):
    mode = [m for m in ALLOWED if getattr(args, m)][0]
    flag = mode.replace("_", "-")
    for name in OPTIONS:
        if getattr(args, name) not in (None, False) and name not in ALLOWED[mode]:
            raise UsageError("--%s is not valid with --%s"
                             % (name.replace("_", "-"), flag))
    for name in REQUIRED[mode]:
        if getattr(args, name) is None:
            raise UsageError("--%s needs --%s" % (flag, name))
    if bool(args.apply) != bool(args.backup):
        raise UsageError("--apply and --backup go together")
    if args.allow_edit_capabilities and not args.apply:
        raise UsageError("--allow-edit-capabilities needs --apply")
    return mode


def main(argv=None):
    """Run the CLI. A fault in this tool exits 70, not 1, because 1 is the
    code a scheduled job reads as 'the settings differ'."""
    try:
        try:
            args = build_parser().parse_args(argv)
        except SystemExit as exc:
            # argparse exits 2 on a flag it refuses, and 2 here means an apply
            # failed. A mistyped --ap is a usage error, so it gets 64.
            return EXIT_USAGE if exc.code == 2 else exc.code
        if validate(args) == "self_test":
            return self_test()
        return run(args)
    except UsageError as exc:
        say("svcparity: error: %s" % exc, err=True)
        return EXIT_USAGE
    except ServiceError as exc:
        say("svcparity: %s" % exc, err=True)
        return EXIT_READ
    except Exception as exc:
        say("svcparity: unexpected %s: %s. This is a fault in the tool, not a "
            "finding about the service." % (type(exc).__name__, exc), err=True)
        traceback.print_exc()
        return EXIT_INTERNAL


# ------------------------------------------------------------------ self-test

SVC1 = "Demo/Roads.MapServer"
SVC2 = "Demo/Zones.MapServer"
SVC3 = "Demo/Vault.MapServer"
ITEM1 = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
ITEM2 = "0f9e8d7c6b5a49382716f5e4d3c2b1a0"


EXTRAS = {"none": {}, "path": {"outputDir": "/srv/out"},
          "secret": {"outputDir": "/srv/out", "dbPassword": "synthetic-db-pass"}}


def fixture_service(name, found=True, extras="path"):
    """A synthetic admin JSON. found=True is the tuned service, False is the
    same service as an overwrite publish leaves it."""
    tuned = found
    return {
        "serviceName": name, "type": "MapServer", "description": "synthetic",
        "capabilities": "Map,Query,Data", "clusterName": "default",
        "provider": "ArcObjects11" if tuned else "DMaps",
        "minInstancesPerNode": 2 if tuned else 1,
        "maxInstancesPerNode": 6 if tuned else 2,
        "maxWaitTime": 60, "maxStartupTime": 300,
        "maxIdleTime": 900 if tuned else 1800, "maxUsageTime": 600,
        "recycleInterval": 24, "loadBalancing": "ROUND_ROBIN",
        "isolationLevel": "HIGH",
        "properties": {
            "maxRecordCount": "4000" if tuned else "1000",
            "maxBufferCount": "100",
            "filePath": "/srv/defs/%s_v1.msd" % name if tuned
            else "/srv/defs/%s_v2.msd" % name,
            "schemaLockingEnabled": "false" if tuned else "true",
        },
        "extensions": [
            {"typeName": "FeatureServer", "enabled": "true",
             "capabilities": "Query" if tuned else "Query,Create,Update",
             "properties": dict({
                 "maxRecordCount": "4000" if tuned else "1000",
                 "syncEnabled": "false"}, **EXTRAS[extras])},
            {"typeName": "KmlServer", "enabled": False,
             "capabilities": "SingleImage,SeparateImages",
             "properties": {"minRefreshPeriod": "30"}},
        ],
        "frameworkProperties": {"javaHeapSize": "64"},
    }


@contextlib.contextmanager
def env_patch(prefix, **kv):
    """Drop every variable that starts with prefix, set kv, and put the
    caller's environment back afterwards."""
    saved = dict(os.environ)
    [os.environ.pop(k) for k in saved if k.startswith(prefix)]
    os.environ.update(kv)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def make_stub():
    """A local stand-in for the Administrator API and the portal item calls.

    It keeps what the Edit Service page says a server keeps: the posted JSON
    replaces the stored one, so a key left out of the request is gone."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import ThreadingMixIn
    from urllib.parse import parse_qsl

    class Stub(object):
        token, ptoken = "tkn-synthetic-0001", "ptkn-synthetic-0002"
        user, password = "admin-demo", "pw-synthetic-0003"

        def __init__(self):
            self.services, self.running, self.items = {}, {}, {}
            self.fault, self.log = {}, []

    stub = Stub()

    def error(message, code=500):
        return {"status": "error", "messages": [message], "code": code}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, doc, raw=None, code=200):
            data = raw if raw is not None else json.dumps(doc).encode("ascii")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def handle_any(self, method):
            parts = urlsplit(self.path)
            query = dict(parse_qsl(parts.query))
            body = {}
            if method == "POST":
                size = int(self.headers.get("Content-Length") or 0)
                body = dict(parse_qsl(self.rfile.read(size).decode("ascii")))
            sent = dict(query)
            sent.update(body)
            stub.log.append((method, parts.path, "token" in query,
                             "token" in body, sorted(body)))
            path = parts.path
            if path == "/arcgis/admin/generateToken":
                if (body.get("username"), body.get("password")) == (
                        stub.user, stub.password):
                    return self.reply({"token": stub.token, "expires": 1})
                return self.reply(error("Invalid username or password %s"
                                        % body.get("password"), 400))
            if method == "POST" and "token" not in body:
                return self.reply(error("Token required", 499))
            if path.startswith("/arcgis/admin/services/"):
                return self.admin(method, path[len("/arcgis/admin/services/"):],
                                  sent, body)
            if path.startswith("/portal/sharing/rest/content/items/"):
                return self.portal(path.split("/")[-2:], sent)
            return self.reply(error("no such resource", 404))

        def admin(self, method, rest, sent, body):
            if sent.get("token") != stub.token:
                return self.reply(error("Invalid token", 498))
            head, _, tail = rest.rpartition("/")
            svc, op = (head, tail) if tail in (
                "status", "edit", "changeProvider") else (rest, "")
            fault = stub.fault
            if svc not in stub.services:
                return self.reply(error("Service %s does not exist" % svc, 404))
            if op == "status":
                real = "STARTED" if stub.running.get(svc, True) else "STOPPED"
                if fault.get("delay", 0) > 0:
                    fault["delay"] -= 1
                    real = "STOPPED"
                if fault.get("never_started"):
                    real = "STOPPED"
                conf = "STARTED" if stub.running.get(svc, True) else "STOPPED"
                return self.reply({"configuredState": conf,
                                   "realTimeState": real})
            if op == "edit":
                if fault.get("edit") == "error":
                    return self.reply(error("busy, token %s" % sent["token"]))
                if fault.get("edit") == "empty":
                    return self.reply({})
                new = json.loads(body["service"])
                old = stub.services[svc]
                for key in fault.get("ignore", []):
                    new["properties"][key] = old["properties"][key]
                stub.services[svc] = new
                return self.reply({"status": "success"})
            if op == "changeProvider":
                if fault.get("provider") == "error" \
                        or body.get("provider") not in PROVIDERS:
                    return self.reply(error("cannot change provider"))
                stub.services[svc]["provider"] = body["provider"]
                return self.reply({"status": "success"})
            kind = fault.get("get")
            if kind == "http500":
                return self.reply(None, raw=b"boom", code=500)
            if kind == "text":
                return self.reply(None, raw=b"<html>not json</html>")
            if kind == "list":
                return self.reply(None, raw=b"[]")
            if kind == "slow":
                import time as real_time
                real_time.sleep(1.2)
            if kind == "bare":
                return self.reply({"error": {"code": 500, "message": "Oops",
                                             "details": ["first", "second"]}})
            if kind == "empty":
                return self.reply({})
            return self.reply(copy.deepcopy(stub.services[svc]))

        def portal(self, tail, sent):
            if sent.get("token") != stub.ptoken:
                return self.reply({"error": {"code": 498,
                                             "message": "Invalid token"}})
            item_id = tail[0] if tail[1] == "groups" else tail[1]
            item = stub.items.get(item_id)
            if item is None:
                return self.reply({"error": {"code": 400,
                                             "message": "Item does not exist"}})
            if tail[1] == "groups":
                return self.reply(item["groups"])
            return self.reply({"id": item_id, "access": item["access"]})

        def do_GET(self):
            self.handle_any("GET")

        def do_POST(self):
            self.handle_any("POST")

    class Server(ThreadingMixIn, HTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address):
            pass

    httpd = Server(("127.0.0.1", 0), Handler)
    stub.url = "http://127.0.0.1:%d" % httpd.server_address[1]
    stub.httpd = httpd
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def stop():
        httpd.shutdown()
        httpd.server_close()

    stub.stop = stop
    stub.services[SVC1] = fixture_service("Roads")
    stub.services[SVC2] = fixture_service("Zones", extras="none")
    stub.services[SVC3] = fixture_service("Vault", extras="secret")
    stub.items[ITEM1] = {"access": "org", "groups": {
        "admin": [{"id": "g1"}], "member": [{"id": "g2"}], "other": []}}
    return stub


def self_test():
    """Run the assertions. No arcpy, no real server, a stub on 127.0.0.1."""
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="svcparity-selftest-")
    stub = make_stub()
    clock = [1000.0]
    saved_timer = (TIMER.now, TIMER.sleep)
    saved_timeout = HTTP_TIMEOUT
    TIMER.now = lambda: clock[0]
    TIMER.sleep = lambda s: clock.__setitem__(0, clock[0] + s)
    try:
        with env_patch("SVCPARITY_", SVCPARITY_TOKEN="ambient-token-9"):
            return _self_test(stub, tmp, clock)
    finally:
        TIMER.now, TIMER.sleep = saved_timer
        globals()["HTTP_TIMEOUT"] = saved_timeout
        stub.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def _self_test(stub, tmp, clock):
    passed = [0]
    failed = []
    seen = []        # every byte any run printed or wrote, for the credential grep

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, exc_type, label):
        try:
            fn()
        except exc_type:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    def cli(argv, **env):
        del SECRETS[:]
        out, err = io.StringIO(), io.StringIO()
        with env_patch("SVCPARITY_", **env),                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        seen.append(out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    tok = {"SVCPARITY_TOKEN": stub.token, "SVCPARITY_PORTAL_TOKEN": stub.ptoken}
    srv = ["--server", stub.url + "/arcgis"]
    por = ["--portal", stub.url + "/portal"]

    def snap(*extra):
        code, out, err = cli(["--snapshot"] + srv + list(extra), **tok)
        return code, (json.loads(out) if code == 0 else None), err

    def tmpfile(name):
        return os.path.join(tmp, name)

    def save(name, doc):
        path = tmpfile(name)
        with io.open(path, "w", encoding="ascii") as fh:
            fh.write(json.dumps(doc))
        return path

    def posts():
        return [e for e in stub.log if e[0] == "POST"]

    def overwrite(svc):
        stub.services[svc] = fixture_service(
            svc.split("/")[1].split(".")[0], found=False,
            extras={SVC1: "path", SVC2: "none", SVC3: "secret"}[svc])

    print("svcparity self-test: no arcpy, no real server, a stub on 127.0.0.1")
    print("-" * 68)
    check(os.environ["SVCPARITY_TOKEN"] == "ambient-token-9",
          "probe: the caller's environment holds a token of its own")

    # ---- the pure core
    tuned = fixture_service("Roads")
    over = fixture_service("Roads", found=False)
    state, withheld = extract_state(tuned)
    secret_doc = fixture_service("Vault", extras="secret")
    secret_state, secret_withheld = extract_state(secret_doc)
    check(norm(True) == "true" and norm(False) == "false" and norm(6) == "6",
          "a boolean and a number compare as the strings a server returns")
    check(caps_key("Query, Create") == caps_key("create,QUERY"),
          "a reordered or re-cased capability list is not a difference")
    check(not same(("pool", "x"), None, "1") and same(("pool", "x"), 1, "1"),
          "an absent live value differs, and 1 equals '1'")
    check(same(("ext", "F", "capabilities"), "Query,Create", "Create,Query")
          and not same(("ext", "F", "properties", "capabilities"), "a,b", "b,a"),
          "capabilities compare as a set only on the extension field, not on "
          "a property that happens to share the name")
    check(state["pool"]["maxIdleTime"] == 900
          and state["properties"]["maxRecordCount"] == "4000"
          and "filePath" not in state["properties"]
          and "description" not in state and "clusterName" not in state["pool"],
          "the state holds the allowlisted keys and not the service "
          "definition path  <-- pinned defect")
    check(secret_withheld == ["ext.FeatureServer.properties.outputDir",
                              "ext.FeatureServer.properties.dbPassword"]
          and withheld == ["ext.FeatureServer.properties.outputDir"]
          and "dbPassword" not in json.dumps(secret_state)
          and "outputDir" not in json.dumps(secret_state),
          "a credential-like or path-like extension property never enters the "
          "state, and is reported by name")
    check(plan(state, tuned) == [], "a service that matches its state plans nothing")
    changes = plan(state, over)
    paths = [".".join(c.path) for c in changes]
    check(paths == ["ext.FeatureServer.capabilities",
                    "ext.FeatureServer.properties.maxRecordCount",
                    "pool.maxIdleTime", "pool.maxInstancesPerNode",
                    "pool.minInstancesPerNode", "properties.maxRecordCount",
                    "properties.schemaLockingEnabled", "provider"],
          "an overwritten service plans exactly the tuned settings it lost")
    check(not any("filePath" in p for p in paths),
          "the plan never touches the definition path the overwrite installed"
          "  <-- pinned defect")
    new = apply_changes(over, changes)
    check(new["properties"]["filePath"] == over["properties"]["filePath"]
          and new["description"] == "synthetic"
          and new["frameworkProperties"] == {"javaHeapSize": "64"},
          "the edit body keeps every key the plan did not name, because the "
          "server drops a key that is left out  <-- pinned defect")
    check(new["maxInstancesPerNode"] == 6 and new["provider"] == "DMaps"
          and over["maxInstancesPerNode"] == 2,
          "the edit body carries the saved value, leaves the provider to "
          "changeProvider, and does not alter its input")
    bare = {"serviceName": "X", "extensions": [
        {"typeName": "F", "enabled": "true", "capabilities": "Query"}]}
    bare_state = {"properties": {"maxRecordCount": "5"}, "extensions": {
        "F": {"enabled": "true", "capabilities": "Query",
              "properties": {"maxRecordCount": "5"}}}}
    filled = apply_changes(bare, plan(bare_state, bare))
    check(filled["properties"] == {"maxRecordCount": "5"}
          and filled["extensions"][0]["properties"] == {"maxRecordCount": "5"},
          "a missing properties object is created, on the service and on an "
          "extension")
    stuck = plan({"provider": "SDS", "extensions": {
        "Gone": {"enabled": "true", "properties": {}}}}, over)
    check([c.how for c in stuck] == ["stuck", "stuck"]
          and all(c.note for c in stuck),
          "an unsupported provider and a missing extension are reported as "
          "not restorable, with a reason")
    check(plan({"provider": "ArcObjects11"}, over)[0].how == "provider",
          "a supported provider change is routed to changeProvider")
    check(edit_exposure(over, over) == []
          and edit_exposure(over, apply_changes(over, plan(
              {"extensions": {"FeatureServer": {
                  "capabilities": "Query,Delete,Sync"}}}, over)))
          == ["FeatureServer:delete", "FeatureServer:sync"],
          "edit exposure names each write capability a restore would add")
    check(edit_exposure(over, apply_changes(over, plan(state, over))) == [],
          "restoring a service to LESS edit access needs no permission"
          "  <-- pinned defect")
    off = copy.deepcopy(over)
    off["extensions"][0]["enabled"] = "false"
    check(edit_exposure(off, over) == ["FeatureServer:create",
                                       "FeatureServer:update"],
          "switching an extension on counts as exposure even when its "
          "capability string did not change  <-- pinned defect")
    saved_items = {ITEM1: {"access": "org", "groups": ["g1", "g2"]}}
    check(diff_sharing(saved_items, saved_items) == []
          and diff_sharing(saved_items, {ITEM1: None})
          == ["item %s: could not be read" % ITEM1]
          and diff_sharing(saved_items, {ITEM1: {
              "access": "public", "groups": ["g1", "g3"]}})
          == ["item %s: access is public, saved org" % ITEM1,
              "item %s: groups lost [g2], gained [g3]" % ITEM1],
          "sharing differences name the access change and the groups lost and gained")
    check([decide(*a) for a in [
        (True, True, True, 0, 1), (False, True, True, 0, 1),
        (False, False, True, 0, 1), (False, False, False, 0, 1),
        (False, False, False, 5, 1), (False, False, False, 5, 0)]]
          == [2, 3, 5, 4, 1, 0],
          "the exit status is the worst thing that happened, and 0 only when "
          "something was compared and matched")
    row = Row(SVC1, over, plan(state, over), None)
    lines = render(row)
    check(lines[0].endswith("8 differences from the snapshot")
          and any("provider" in l and "live DMaps, saved ArcObjects11" in l
                  for l in lines)
          and render(Row(SVC1, tuned, [], None))
          == ["Demo/Roads.MapServer: matches the snapshot"]
          and render(Row(SVC1, None, [], "boom"))
          == ["Demo/Roads.MapServer: UNREADABLE: boom"]
          and show(None) == "<absent>",
          "the report reads the same three ways: differs, matches, unreadable")
    check(any("NOT RESTORABLE" in l for l in render(Row(SVC1, over, stuck, None))),
          "a change the tool cannot make says so on its report line")
    check(reply_message({"error": {"message": "Oops", "details": ["a", "b"]}})
          == "Oops; a; b" and reply_message({"messages": ["x"]}) == "x"
          and reply_message({}) == "no message",
          "an error body is read in both shapes the server uses")

    # ---- files and the command line
    good_doc = {"format": FORMAT, "services": {SVC1: state}, "items": {}}
    check(load_state(save("good.json", good_doc))["services"][SVC1] == state,
          "a snapshot file loads")
    bad_docs = [
        ("bad format", dict(good_doc, format="other/1")),
        ("a service name that walks out of the folder",
         dict(good_doc, services={"../x.MapServer": state})),
        ("a service state of the wrong shape",
         dict(good_doc, services={SVC1: {"pool": []}})),
        ("an extension of the wrong shape",
         dict(good_doc, services={SVC1: {"extensions": {"F": []}}})),
        ("an extension property list of the wrong shape",
         dict(good_doc, services={SVC1: {"extensions": {
             "F": {"properties": []}}}})),
        ("a service state that is not an object",
         dict(good_doc, services={SVC1: []})),
        ("an item id that is not 32 hex digits",
         dict(good_doc, items={"abc": {"access": "org", "groups": []}})),
        ("an item of the wrong shape",
         dict(good_doc, items={ITEM1: {"access": "org", "groups": [1]}})),
        ("a list", []),
    ]
    for label, doc in bad_docs:
        raises(lambda doc=doc: load_state(save("bad.json", doc)), UsageError,
               "a snapshot with %s is refused" % label)
    with io.open(tmpfile("junk.json"), "w") as fh:
        fh.write("{not json")
    raises(lambda: load_state(tmpfile("junk.json")), UsageError,
           "a file that is not JSON is refused")
    raises(lambda: load_state(tmpfile("absent.json")), UsageError,
           "a file that does not exist is refused")
    check(check_url("https://h.example/arcgis/") == "https://h.example/arcgis"
          and check_url("http://127.0.0.1:6080/arcgis")
          and check_url("http://localhost/arcgis"),
          "https is accepted anywhere, plain http only on loopback, and a "
          "trailing slash is dropped")
    for url in ("http://gis.example.com/arcgis", "gis.example.com", "ftp://x/y",
                "https://"):
        raises(lambda url=url: check_url(url), UsageError,
               "%s is refused, so a credential never leaves in clear text"
               % url)
    with env_patch("https_proxy", https_proxy="http://proxy.invalid:3128"):
        loop_proxies = [h.proxies for h in opener_for("http://127.0.0.1:1/")
                        .handlers if isinstance(h, ProxyHandler)]
        far_proxies = [h.proxies for h in opener_for("https://gis.example.com/")
                       .handlers if isinstance(h, ProxyHandler)]
    check(loop_proxies == [] and "https" in far_proxies[0],
          "loopback traffic never goes through a proxy, other traffic keeps the "
          "system setting")

    code, out, err = cli(["--restore", "--ap"], **tok)
    check(code == 64 and not posts(),
          "a unique prefix --ap of --apply is REFUSED by the parser and "
          "writes nothing  <-- pinned defect")
    code, out, err = cli(["--restore", "--appl", "--state", "x"] + srv, **tok)
    check(code == 64, "--appl is refused too  <-- pinned defect")
    check(build_parser().allow_abbrev is False,
          "the parser is built with allow_abbrev=False  <-- pinned defect")
    code, out, err = cli(["--self-test", "--apply"])
    check(code == 64 and "--apply is not valid with --self-test" in err,
          "a flag that does not belong to the mode is named and refused")
    code, out, err = cli([])
    check(code == 64, "no mode at all is a usage error")
    code, out, err = cli(["--diff", "--server", "https://h.example"])
    check(code == 64 and "--diff needs --state" in err,
          "a mode without its required flag is named and refused")
    code, out, err = cli(["--restore", "--state", "x", "--server", "https://h",
                          "--apply"])
    check(code == 64 and "--apply and --backup go together" in err,
          "--apply without --backup is refused, so no restore runs without an undo")
    code, out, err = cli(["--restore", "--state", "x", "--server", "https://h",
                          "--backup", "b"])
    check(code == 64, "--backup without --apply is refused")
    code, out, err = cli(["--restore", "--state", "x", "--server", "https://h",
                          "--allow-edit-capabilities"])
    check(code == 64 and "needs --apply" in err,
          "--allow-edit-capabilities without --apply is refused")
    code, out, err = cli(["--snapshot", "--server", "https://h", "--service",
                          "../Roads.MapServer"], SVCPARITY_TOKEN="t")
    check(code == 64 and "not Folder/Name.Type" in err,
          "a service name that is not Folder/Name.Type is refused before any request")
    code, out, err = cli(["--snapshot", "--server", "https://h", "--service",
                          SVC1, "--item", ITEM1], SVCPARITY_TOKEN="t")
    check(code == 64 and "--item and --portal go together" in err,
          "an item without a portal is refused")
    code, out, err = cli(["--snapshot", "--server", "https://h", "--service",
                          SVC1, "--portal", "https://p", "--item", "xyz"],
                         SVCPARITY_TOKEN="t")
    check(code == 64 and "32 character" in err,
          "an item id that is not 32 hex digits is refused")
    code, out, err = cli(["--snapshot", "--server", "http://gis.example.com",
                          "--service", SVC1], SVCPARITY_TOKEN="t")
    check(code == 64 and "https://" in err,
          "plain http to a remote server is refused with the reason")
    code, out, err = cli(["--snapshot"] + srv + ["--service", SVC1])
    check(code == 64 and "SVCPARITY_TOKEN" in err and not stub.log,
          "no credential in the environment is a usage error before any "
          "request, and the caller's own token is not borrowed")
    code, out, err = cli(["--help"])
    check(code == 0 and "--snapshot" in out, "--help prints the usage and exits 0")

    # ---- snapshot
    code, doc, err = snap("--service", SVC1, "--service", SVC2)
    check(code == 0 and sorted(doc["services"]) == [SVC1, SVC2]
          and doc["services"][SVC1] == state and doc["format"] == FORMAT,
          "--snapshot prints the settings of every named service as JSON")
    check("left out of the snapshot: ext.FeatureServer.properties.outputDir"
          in err and doc["withheld"][SVC1] == withheld
          and SVC2 not in doc["withheld"],
          "and says on stderr which extension properties it left out, by name")
    code, doc, err = snap("--service", SVC1, *(por + ["--item", ITEM1]))
    check(code == 0 and doc["items"][ITEM1] == {
        "access": "org", "groups": ["g1", "g2"]},
          "--item records the access level and the groups visible to the token")
    code, doc, err = snap("--service", SVC3)
    check(code == 0 and "synthetic-db-pass" not in json.dumps(doc)
          and "synthetic-db-pass" not in err
          and doc["withheld"][SVC3] == secret_withheld,
          "a snapshot of a service that holds a credential names the key it "
          "left out and never carries the value")
    snap_path = tmpfile("before.json")
    code, out, err = cli(["--snapshot"] + srv + ["--service", SVC1, "--service",
                                                  SVC2] + por + ["--item", ITEM1],
                         **tok)
    with io.open(snap_path, "w", encoding="ascii") as fh:
        fh.write(out)

    stub.fault["get"] = "http500"
    code, out, err = cli(["--snapshot"] + srv + ["--service", SVC1], **tok)
    check(code == 3 and out == "" and "HTTP Error 500" in err,
          "a server error exits 3 and prints no snapshot at all")
    stub.fault["get"] = "text"
    code, out, err = cli(["--snapshot"] + srv + ["--service", SVC1], **tok)
    check(code == 3 and "did not return JSON" in err, "a reply that is not JSON exits 3")
    stub.fault["get"] = "list"
    code, out, err = cli(["--snapshot"] + srv + ["--service", SVC1], **tok)
    check(code == 3 and "not return a JSON object" in err,
          "a JSON reply that is not an object exits 3")
    stub.fault["get"] = "bare"
    code, out, err = cli(["--snapshot"] + srv + ["--service", SVC1], **tok)
    check(code == 3 and "Oops; first; second" in err,
          "a 200 with an error body is an error, which a status check misses"
          "  <-- pinned defect")
    stub.fault["get"] = "empty"
    code, out, err = cli(["--snapshot"] + srv + ["--service", SVC1], **tok)
    check(code == 3 and "not a service description" in err,
          "a 200 that is not a service description exits 3, not an empty snapshot"
          "  <-- pinned defect")
    stub.fault["get"] = "slow"
    globals()["HTTP_TIMEOUT"] = 0.4
    code, out, err = cli(["--snapshot"] + srv + ["--service", SVC1], **tok)
    globals()["HTTP_TIMEOUT"] = 60
    check(code == 3 and "timed out" in err,
          "a server that does not answer is reported, not waited on forever")
    stub.fault.clear()
    code, out, err = cli(["--snapshot"] + srv + ["--service",
                                                 "Demo/Nope.MapServer"], **tok)
    check(code == 3 and "does not exist" in err,
          "a service the server does not have exits 3 with the server's words")
    code, out, err = cli(["--snapshot"] + srv + ["--service", SVC1] + por +
                         ["--item", ITEM2], **tok)
    check(code == 3 and "Item does not exist" in err,
          "an item the portal does not have exits 3")
    code, out, err = cli(["--snapshot"] + srv + ["--service", SVC1] + por +
                         ["--item", ITEM1], SVCPARITY_TOKEN=stub.token,
                         SVCPARITY_PORTAL_TOKEN="bad-portal-token-5")
    check(code == 3 and "Invalid token" in err and "bad-portal" not in err,
          "a portal token the portal rejects exits 3 and is not printed")
    raises(lambda: http_json("POST", stub.url + "/arcgis/admin/services/"
                             + SVC1 + "/edit", {"f": "json"}), ServiceError,
           "a POST that carries no token in its body is refused by the server")
    raises(lambda: http_json("GET", stub.url + "/nowhere", {}), ServiceError,
           "a path the server does not have is an error, not an empty answer")
    stub.items[ITEM2] = {"access": None, "groups": {}}
    code, out, err = cli(["--snapshot"] + srv + ["--service", SVC1] + por +
                         ["--item", ITEM2], **tok)
    check(code == 3 and "not an item" in err, "an item reply without an access level exits 3")
    del stub.items[ITEM2]
    dead = make_stub()
    dead_url = dead.url
    dead.stop()
    code, out, err = cli(["--snapshot", "--server", dead_url + "/arcgis",
                          "--service", SVC1], **tok)
    check(code == 3 and "failed" in err, "a refused connection exits 3")
    code, out, err = cli(["--snapshot", "--server",
                          stub.url.replace("http:", "https:") + "/arcgis",
                          "--service", SVC1], **tok)
    check(code == 3 and "failed" in err,
          "a TLS failure exits 3 and there is no flag to switch verification off")

    # ---- credentials
    stub.log[:] = []
    code, out, err = cli(["--snapshot"] + srv + ["--service", SVC1],
                         SVCPARITY_USER=stub.user,
                         SVCPARITY_PASSWORD=stub.password)
    mint = [e for e in stub.log if e[1].endswith("generateToken")]
    check(code == 0 and len(mint) == 1 and mint[0][0] == "POST"
          and not mint[0][2] and "password" in mint[0][4]
          and "token" not in mint[0][4],
          "a token is minted from a user and password, which travel in a POST "
          "body and never in a URL")
    code, out, err = cli(["--snapshot"] + srv + ["--service", SVC1],
                         SVCPARITY_USER=stub.user, SVCPARITY_PASSWORD="wrong-pw-77")
    check(code == 3 and "Invalid username or password" in err
          and "wrong-pw-77" not in err,
          "a rejected login exits 3 and the password the server echoed back is masked"
          "  <-- pinned defect")
    stub.token, real = None, stub.token
    code, out, err = cli(["--snapshot"] + srv + ["--service", SVC1],
                         SVCPARITY_USER=stub.user, SVCPARITY_PASSWORD=stub.password)
    stub.token = real
    check(code == 3 and "no token" in err,
          "a login that returns no token exits 3")
    stub.token = 5
    code, out, err = cli(["--snapshot"] + srv + ["--service", SVC1],
                         SVCPARITY_USER=stub.user, SVCPARITY_PASSWORD=stub.password)
    stub.token = real
    check(code == 3, "a non-string token exits 3")
    code, out, err = cli(["--snapshot"] + srv + ["--service", SVC1],
                         SVCPARITY_TOKEN="expired-token-1")
    check(code == 3 and "Invalid token" in err and "expired-token-1" not in err,
          "an expired token exits 3 and is not printed")

    # ---- diff
    code, out, err = cli(["--diff", "--state", snap_path] + srv + por, **tok)
    check(code == 0 and "VERDICT: MATCH" in out
          and out.count("matches the snapshot") == 2,
          "--diff of an untouched site matches and exits 0")
    overwrite(SVC1)
    stub.log[:] = []
    code, out, err = cli(["--diff", "--state", snap_path] + srv + por, **tok)
    check(code == 1 and "VERDICT: DIFFERS" in out
          and "8 differences from the snapshot" in out
          and "provider" in out and "live DMaps, saved ArcObjects11" in out,
          "--diff after an overwrite lists each lost setting and exits 1")
    check("Plan only" not in out and not posts(),
          "--diff never prints a restore hint and never writes")
    stub.items[ITEM1] = {"access": "public", "groups": {
        "admin": [], "member": [{"id": "g2"}, {"id": "g9"}], "other": []}}
    code, out, err = cli(["--diff", "--state", snap_path] + srv + por, **tok)
    check("sharing (read-only, never restored): item %s: access is public, "
          "saved org" % ITEM1 in out
          and "groups lost [g1], gained [g9]" in out,
          "sharing is diffed from the portal and reported as read-only")
    del stub.items[ITEM1]
    code, out, err = cli(["--diff", "--state", snap_path] + srv + por, **tok)
    check(code == 3 and "could not be read" in out and "VERDICT: UNREADABLE" in out,
          "an item the portal no longer has exits 3, not 1")
    stub.items[ITEM1] = {"access": "org", "groups": {
        "admin": [{"id": "g1"}], "member": [{"id": "g2"}], "other": []}}
    code, out, err = cli(["--diff", "--state", snap_path] + srv, **tok)
    check(code == 64 and "--portal goes with" in err,
          "a snapshot with items and no --portal is refused, not half checked")
    plain = save("plain.json", good_doc)
    code, out, err = cli(["--diff", "--state", plain] + srv + por, **tok)
    check(code == 64, "--portal with a snapshot that holds no items is refused")
    gone = save("gone.json", dict(good_doc, services={
        SVC1: state, "Demo/Gone.MapServer": state}))
    code, out, err = cli(["--diff", "--state", gone] + srv, **tok)
    check(code == 3 and "Demo/Gone.MapServer: UNREADABLE" in out
          and "Demo/Roads.MapServer: 8 differences" in out,
          "one unreadable service is reported and the others are still checked")
    empty = save("empty.json", {"format": FORMAT, "services": {}, "items": {}})
    code, out, err = cli(["--diff", "--state", empty] + srv, **tok)
    check(code == 4 and "VERDICT: UNVERIFIED" in out,
          "a snapshot with nothing in it compares nothing and exits 4, "
          "not a clean 0  <-- pinned defect")
    nothing = save("nothing.json", dict(good_doc, services={SVC1: {}}))
    code, out, err = cli(["--diff", "--state", nothing] + srv, **tok)
    check(code == 4, "a service entry with no settings in it exits 4 too")

    # ---- restore: plan
    stub.log[:] = []
    before_plan = copy.deepcopy(stub.services)
    code, out, err = cli(["--restore", "--state", snap_path] + srv + por, **tok)
    check(code == 1 and "Plan only. Nothing was written" in out
          and not posts() and stub.services == before_plan,
          "--restore without --apply plans, prints it, and writes nothing")

    # ---- restore: refusals before any write
    exists = tmpfile("exists.json")
    with io.open(exists, "w") as fh:
        fh.write("{}")
    code, out, err = cli(["--restore", "--state", snap_path, "--apply",
                          "--backup", exists] + srv + por, **tok)
    check(code == 64 and "must not exist" in err and not posts(),
          "a backup file that already exists is refused before any write, "
          "so one undo is never replaced by the next")
    edit_state = copy.deepcopy(state)
    edit_state["extensions"]["FeatureServer"]["capabilities"] = "Query,Create"
    editable = save("editable.json", dict(good_doc, services={SVC1: edit_state}))
    stub.services[SVC1] = fixture_service("Roads", found=False)
    stub.services[SVC1]["extensions"][0]["capabilities"] = "Query"
    code, out, err = cli(["--restore", "--state", editable, "--apply", "--backup",
                          tmpfile("never.json")] + srv, **tok)
    check(code == 5 and "REFUSED: Demo/Roads.MapServer would turn on edit "
          "capabilities that are off now: FeatureServer:create" in err
          and not posts()
          and not os.path.exists(tmpfile("never.json")),
          "a restore that would turn edit capabilities on is refused, writes "
          "nothing and leaves no backup  <-- pinned defect")
    code, out, err = cli(["--restore", "--state", editable, "--apply", "--backup",
                          tmpfile("allowed.json"), "--allow-edit-capabilities"]
                         + srv, **tok)
    check(code == 0 and stub.services[SVC1]["extensions"][0]["capabilities"]
          == "Query,Create",
          "--allow-edit-capabilities lets that same restore through")
    overwrite(SVC1)

    # ---- restore: the real thing
    stub.log[:] = []
    backup = tmpfile("undo.json")
    pre = copy.deepcopy(stub.services[SVC1])
    code, out, err = cli(["--restore", "--state", snap_path, "--apply",
                          "--backup", backup] + srv + por, **tok)
    live = stub.services[SVC1]
    check(code == 0 and "VERDICT: MATCH" in out and "restored and read back" in out,
          "--restore --apply puts the pool, instance, maxRecordCount and "
          "extension settings back and reads them back")
    check(live["provider"] == "ArcObjects11" and live["minInstancesPerNode"] == 2
          and live["maxInstancesPerNode"] == 6 and live["maxIdleTime"] == 900
          and live["properties"]["maxRecordCount"] == "4000"
          and live["extensions"][0]["capabilities"] == "Query"
          and live["extensions"][0]["properties"]["maxRecordCount"] == "4000"
          and live["properties"]["schemaLockingEnabled"] == "false",
          "every one of those settings holds on the server afterwards")
    check(live["properties"]["filePath"] == pre["properties"]["filePath"]
          and live["description"] == "synthetic"
          and live["frameworkProperties"] == {"javaHeapSize": "64"}
          and live["extensions"][0]["properties"]["outputDir"] == "/srv/out",
          "the overwrite itself is kept: the new definition path survives, and "
          "so does every key the tool does not own  <-- pinned defect")
    ops = [e[1].rsplit("/", 1)[1] for e in posts()]
    check(ops == ["changeProvider", "edit"],
          "the provider is changed first through changeProvider, then one edit "
          "carries everything else")
    with io.open(backup, encoding="ascii") as fh:
        undo = fh.read()
    seen.append(undo)
    undo_doc = json.loads(undo)
    check(undo_doc["format"] == FORMAT and list(undo_doc["services"]) == [SVC1]
          and undo_doc["services"][SVC1]["provider"] == "DMaps"
          and "outputDir" not in undo and "filePath" not in undo,
          "the backup holds the state as it was before the restore, for only "
          "the services that needed one, and no path")
    gets = [e for e in stub.log if e[0] == "GET"]
    check(gets and all(e[2] and not e[3] for e in gets)
          and all(e[3] and not e[2] for e in posts()),
          "every GET carries the token in the query and every POST in the "
          "body, never the other way round")
    code, doc, err = snap("--service", SVC1, "--service", SVC2)
    with io.open(snap_path, encoding="ascii") as fh:
        original = json.load(fh)
    check(code == 0 and doc["services"] == original["services"],
          "a snapshot taken after the restore equals the one taken before the "
          "overwrite")
    stub.log[:] = []
    gone_backup = tmpfile("none.json")
    code, out, err = cli(["--restore", "--state", snap_path, "--apply",
                          "--backup", gone_backup] + srv + por, **tok)
    check(code == 0 and not posts() and not os.path.exists(gone_backup),
          "a second run finds nothing to do, writes nothing and creates no backup")

    # ---- restore: the undo path
    def one_service_state():
        return save("one.json", dict(good_doc, services={SVC1: state}))

    def attempt(label, fault, expect_text, put_back=True, delay=None):
        overwrite(SVC1)
        before = copy.deepcopy(stub.services[SVC1])
        stub.fault.clear()
        stub.fault.update(fault)
        path = tmpfile("undo-%s.json" % label)
        code, out, err = cli(["--restore", "--state", one_service_state(),
                              "--apply", "--backup", path] + srv, **tok)
        stub.fault.clear()
        check(code == 2 and expect_text in err and "VERDICT: FAILED" in out,
              "%s: exits 2 and says why" % label)
        if put_back:
            check(stub.services[SVC1] == before and "Put back as found" in err,
                  "%s: the service is put back as it was found" % label)
        return err

    err = attempt("a rejected edit", {"edit": "error"}, "busy, token ***")
    check(stub.token not in err, "the token the server echoed is masked")
    attempt("an edit that reports nothing", {"edit": "empty"},
            "did not report success")
    attempt("a setting that does not read back", {"ignore": ["maxRecordCount"]},
            "did not read back: properties.maxRecordCount")
    attempt("a provider the server refuses", {"provider": "error"},
            "cannot change provider")
    err = attempt("a service that never comes back", {"never_started": True},
                  "still STOPPED after 120 seconds", put_back=False)
    check("revert failed" in err and "NOT put back: use the backup file" in err,
          "a revert that also fails says so and points at the backup file")

    overwrite(SVC1)
    stub.fault["delay"] = 3
    clock[0] = 1000.0
    code, out, err = cli(["--restore", "--state", one_service_state(), "--apply",
                          "--backup", tmpfile("undo-slow.json")] + srv, **tok)
    check(code == 0 and clock[0] >= 1000.0 + 2 * POLL_SECONDS,
          "a service that takes a few polls to come back is waited for")
    stub.fault.clear()

    overwrite(SVC1)
    stub.running[SVC1] = False
    code, out, err = cli(["--restore", "--state", one_service_state(), "--apply",
                          "--backup", tmpfile("undo-stopped.json")] + srv, **tok)
    check(code == 0 and stub.running[SVC1] is False
          and stub.services[SVC1]["maxInstancesPerNode"] == 6,
          "a service that was stopped is restored and left stopped, and the "
          "wait does not time out")
    stub.running.clear()

    # ---- restore: a service that carries a credential-like key
    overwrite(SVC3)
    vault_snap = tmpfile("vault.json")
    with io.open(vault_snap, "w", encoding="ascii") as fh:
        fh.write(json.dumps({"format": FORMAT, "items": {}, "services": {
            SVC3: extract_state(fixture_service("Vault", extras="secret"))[0]}}))
    stub.log[:] = []
    code, out, err = cli(["--restore", "--state", vault_snap] + srv, **tok)
    check(code == 1 and "VERDICT: DIFFERS" in out and not posts(),
          "a service with a credential-like key is still diffed and planned")
    code, out, err = cli(["--restore", "--state", vault_snap, "--apply",
                          "--backup", tmpfile("undo-vault.json"),
                          "--allow-edit-capabilities"] + srv, **tok)
    check(code == 5 and "REFUSED: Demo/Vault.MapServer carries credential-like "
          "keys (/extensions/properties/dbPassword)" in err and not posts()
          and not os.path.exists(tmpfile("undo-vault.json"))
          and stub.services[SVC3]["maxInstancesPerNode"] == 2,
          "a service whose JSON carries a credential-like key is never edited, "
          "because the edit call would resend a masked value  <-- pinned defect")
    check(secret_keys({"a": [{"apiKey": 1, "b": {"token": 2}}], "c": "d"})
          == ["/a/apiKey", "/a/b/token"] and secret_keys("x") == [],
          "credential-like keys are found at any depth, in lists and objects")

    # ---- restore: what it cannot do
    overwrite(SVC1)
    stub.services[SVC1]["extensions"] = [
        e for e in stub.services[SVC1]["extensions"] if e["typeName"] != "FeatureServer"]
    stub.log[:] = []
    code, out, err = cli(["--restore", "--state", one_service_state(), "--apply",
                          "--backup", tmpfile("undo-stuck.json")] + srv, **tok)
    check(code == 1 and "NOT RESTORABLE: the live service has no such extension"
          in out and stub.services[SVC1]["maxInstancesPerNode"] == 6,
          "an extension the live service lacks is reported, everything else is "
          "restored, and the exit status says differences remain")
    overwrite(SVC1)
    only = save("only.json", dict(good_doc, services={SVC1: {
        "provider": "SDS"}}))
    stub.log[:] = []
    code, out, err = cli(["--restore", "--state", only, "--apply", "--backup",
                          tmpfile("undo-only.json")] + srv, **tok)
    check(code == 1 and not posts()
          and not os.path.exists(tmpfile("undo-only.json")),
          "a restore with only unrestorable changes writes nothing and "
          "creates no backup")

    # ---- the harness and the footer
    mark = len(failed)
    quiet = io.StringIO()
    with contextlib.redirect_stdout(quiet):
        check(False, "probe: this must register as a failure")
        raises(lambda: None, ValueError, "probe: no error must fail")
        raises(lambda: [][0], ValueError, "probe: the wrong error must fail")
    probe = failed[mark:]
    del failed[mark:]
    check(len(probe) == 3,
          "check() and raises() really do record a failure  <-- pinned defect")
    raises(lambda: (_ for _ in ()).throw(ValueError("x")), ValueError,
           "and raises() accepts the exception it is looking for")

    def summary(passed_count, failures):
        print("-" * 68)
        total = passed_count + len(failures)
        if failures:
            print("%d assertions, %d failed" % (total, len(failures)))
            for f in failures:
                print("  FAILED: %s" % f)
            return 1
        print("%d assertions, 0 failed" % total)
        return 0

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        red = summary(1, ["probe one", "probe two"])
    check(red == 1 and "3 assertions, 2 failed" in buf.getvalue()
          and "  FAILED: probe two" in buf.getvalue(),
          "the footer reports failures by count and by name, and exits 1"
          "  <-- pinned defect")

    real_run = run
    globals()["run"] = lambda args: 1 // 0
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            code = main(["--diff", "--state", "x", "--server", "https://h"])
    finally:
        globals()["run"] = real_run
    check(code == 70,
          "a fault in the tool exits 70, not the 1 that means the settings differ"
          "  <-- pinned defect")

    spec = importlib.util.spec_from_file_location("svcparity_imported",
                                                  os.path.abspath(__file__))
    imported = importlib.util.module_from_spec(spec)
    buf = io.StringIO()
    keep_flag, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        with contextlib.redirect_stdout(buf):
            spec.loader.exec_module(imported)
    finally:
        sys.dont_write_bytecode = keep_flag
    check(buf.getvalue() == "" and callable(imported.plan),
          "importing the module prints nothing and exposes the core")

    blob = "\n".join(seen)
    check(not any(s in blob for s in (stub.token, stub.ptoken, stub.password,
                                      "synthetic-db-pass", "wrong-pw-77",
                                      "expired-token-1")),
          "no token, password or extension credential appears in anything any "
          "run above printed or wrote")

    return summary(passed[0], failed)


if __name__ == "__main__":
    sys.exit(main())
