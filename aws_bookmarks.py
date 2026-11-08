#!/usr/bin/env python3
"""AWS switch-role bookmarks generator.

Reads the account list and OU structure from AWS Organizations, enriches each
account with metadata (short name, business domain, environment) from a
DynamoDB table, and writes a single self-contained HTML page of switch-role
links. The page groups accounts by business domain or by org hierarchy, keeps
the list of roles in browser localStorage, and can export browser bookmarks.

Read-only against AWS: the only write is the local HTML output file. Each run
writes a new timestamped render (output/aws-bookmarks-<UTC timestamp>.html) so
multiple versions can be compared side by side.

The only third-party import is boto3. `truststore` is used if present (OS PKI
store for TLS) but is not required.

The table is configured by ARN (BM_DDB_TABLE_ARN in .env, or --ddb-table-arn); its
name, account and region come from the ARN. It is read with --ddb-profile if given,
else with the Organizations profile (same account) or a role assumed from it.

Multiple orgs: pass one env file per org (--env-file prod.env --env-file lab.env). One
file (or none, meaning ./.env) gives a single-org page; two or more give one combined page
with each org as a top-level group.

Usage (CLI):
    python aws_bookmarks.py --profile org-mgmt
    python aws_bookmarks.py --profile org-mgmt --ddb-profile shared-svcs \
        --ddb-table-arn arn:aws:dynamodb:us-east-1:111122223333:table/account-mapping
    python aws_bookmarks.py --env-file prod.env --env-file lab.env

Usage (library):
    from aws_bookmarks import generate
    generate(profile="org-mgmt", ddb_profile="shared-svcs")
    generate(env_files=["prod.env", "lab.env"])
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

try:  # optional: use the OS certificate store when available
    import truststore

    truststore.inject_into_ssl()
except Exception:
    pass

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

log = logging.getLogger("aws_bookmarks")

ENVIRONMENTS = ("Lab", "Sbx", "Dev", "Tst", "Prd", "Bil")
ENV_ALIASES = {
    "lab": "Lab",
    "sbx": "Sbx",
    "sandbox": "Sbx",
    "dev": "Dev",
    "development": "Dev",
    "tst": "Tst",
    "test": "Tst",
    "testing": "Tst",
    "prd": "Prd",
    "prod": "Prd",
    "production": "Prd",
    "bil": "Bil",
    "billing": "Bil",
}
LEGACY_DOMAIN = "Legacy"
INACTIVE_STATES = {"SUSPENDED", "PENDING_CLOSURE", "CLOSED"}
SIGNIN_URLS = {
    "aws": "https://signin.aws.amazon.com",
    "aws-us-gov": "https://signin.amazonaws-us-gov.com",
    "aws-cn": "https://signin.amazonaws.cn",
}

ROLE_ARN_PREFIX_RE = re.compile(r"^arn:[^:]+:iam::\d{12}:role/")  # same rule as the page's Add form
TABLE_ARN_RE = re.compile(
    r"^arn:(?P<partition>[^:]+):dynamodb:(?P<region>[^:]+):(?P<account>\d{12}):table/(?P<table>[^/]+)$"
)

# kwarg name -> (env var, default)
SETTINGS = {
    "profile": ("AWS_PROFILE", None),
    "region": ("AWS_REGION", "us-east-1"),
    "ddb_table_arn": ("BM_DDB_TABLE_ARN", None),
    "ddb_profile": ("BM_DDB_PROFILE", None),
    "ddb_role_name": ("BM_DDB_ROLE_NAME", "OrganizationAccountAccessRole"),
    "attr_account_id": ("BM_ATTR_ACCOUNT_ID", "accountNumber"),
    "attr_short_name": ("BM_ATTR_SHORT_NAME", "accountShortCode"),
    "attr_ou": ("BM_ATTR_OU", "ou"),
    "attr_environment": ("BM_ATTR_ENVIRONMENT", "environment"),
    "roles": ("BM_DEFAULT_ROLES", "ReadOnly,Administrator"),
    "title": ("BM_TITLE", "AWS Accounts"),
    "output_dir": ("BM_OUTPUT_DIR", "output"),
    "include_inactive": ("BM_INCLUDE_INACTIVE", "false"),
    "org_label": ("BM_ORG_LABEL", None),
    "org_roles": ("BM_ORG_ROLES", None),
    "omit_global_roles": ("BM_ORG_OMIT_GLOBAL_ROLES", "false"),
}
# Settings that apply to the whole page; all others are per org (per env file).
PAGE_SETTINGS = {"roles", "title", "output_dir"}


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def read_dotenv(path: Path) -> Dict[str, str]:
    """Parse an env file (KEY=VALUE lines) without touching os.environ."""
    values: Dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader. Existing env vars win."""
    if path.is_file():
        for key, value in read_dotenv(path).items():
            os.environ.setdefault(key, value)


def _role_list(value: Union[str, Sequence[str]]) -> List[str]:
    """Comma string or list -> trimmed role names (role ARNs reduced to the name), no duplicates."""
    items = value.split(",") if isinstance(value, str) else value
    names = (ROLE_ARN_PREFIX_RE.sub("", str(r).strip()) for r in items)
    return list(dict.fromkeys(n for n in names if n))


def resolve_settings(kwargs: Dict[str, Any], env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """kwargs > `env` (an explicitly named env file) > environment (incl. ./.env) > defaults.

    A key present in `env` but blank means the default, not the shell's value. BM_ORG_* keys
    are per org, so with a named env file they come only from that file."""
    unknown = set(kwargs) - set(SETTINGS)
    if unknown:
        raise TypeError(f"Unknown setting(s): {', '.join(sorted(unknown))}")
    cfg: Dict[str, Any] = {}
    for key, (env_var, default) in SETTINGS.items():
        value = kwargs.get(key)
        if value is None:
            if env is not None and (env_var in env or env_var.startswith("BM_ORG_")):
                value = env.get(env_var) or default
            else:
                value = os.getenv(env_var) or default
        cfg[key] = value
    for key in ("roles", "org_roles"):
        cfg[key] = _role_list(cfg[key] or [])
    for key in ("include_inactive", "omit_global_roles"):
        if isinstance(cfg[key], str):
            cfg[key] = cfg[key].lower() in ("1", "true", "yes")
    if not cfg["ddb_table_arn"]:
        raise ValueError("ddb_table_arn is required (BM_DDB_TABLE_ARN in .env, --ddb-table-arn, or kwarg)")
    m = TABLE_ARN_RE.match(cfg["ddb_table_arn"].strip())
    if not m:
        raise ValueError(f"Not a DynamoDB table ARN: {cfg['ddb_table_arn']}")
    cfg["ddb_table"], cfg["ddb_account_id"], cfg["ddb_region"] = m["table"], m["account"], m["region"]
    return cfg


# --------------------------------------------------------------------------- #
# AWS helpers
# --------------------------------------------------------------------------- #
def get_session(profile: Optional[str], region: str) -> boto3.Session:
    return boto3.Session(profile_name=profile, region_name=region)


def get_client(service: str, session: boto3.Session, region: Optional[str] = None):
    config = Config(retries={"max_attempts": 8, "mode": "adaptive"})
    return session.client(service, region_name=region or session.region_name, config=config)


def normalize_account_id(value: Any) -> str:
    """DDB may store IDs as numbers, dropping leading zeros."""
    text = str(value).strip()
    if text.endswith(".0"):  # Decimal/float rendering
        text = text[:-2]
    return text.zfill(12)


def normalize_env(value: Any) -> Optional[str]:
    if value is None:
        return None
    return ENV_ALIASES.get(str(value).strip().lower())


def _paginate(client, op: str, key: str, **params) -> Iterable[dict]:
    for page in client.get_paginator(op).paginate(**params):
        yield from page.get(key, [])


# --------------------------------------------------------------------------- #
# Organizations
# --------------------------------------------------------------------------- #
def fetch_org(org, include_inactive: bool = False) -> Dict[str, Any]:
    """Walk the org tree. Returns {"tree": node, "accounts": {id: info}}.

    node = {"id", "name", "type": "ROOT"|"OU", "children": [node], "accounts": [id]};
    the root node also has "orgId" (o-...), or None if it can't be read.
    """
    accounts: Dict[str, Dict[str, Any]] = {}
    skipped = 0

    def walk(parent_id: str, name: str, node_type: str, path: List[str]) -> Dict[str, Any]:
        nonlocal skipped
        node = {"id": parent_id, "name": name, "type": node_type, "children": [], "accounts": []}
        for acct in _paginate(org, "list_accounts_for_parent", "Accounts", ParentId=parent_id):
            state = acct.get("State") or acct.get("Status")
            if state in INACTIVE_STATES and not include_inactive:
                skipped += 1
                continue
            accounts[acct["Id"]] = {
                "id": acct["Id"],
                "name": acct.get("Name", acct["Id"]),
                "email": acct.get("Email", ""),
                "state": state,
                "orgPath": path,
            }
            node["accounts"].append(acct["Id"])
        ous = list(_paginate(org, "list_organizational_units_for_parent", "OrganizationalUnits", ParentId=parent_id))
        for ou in sorted(ous, key=lambda o: o["Name"].lower()):
            log.debug("Walking OU %s (%s)", ou["Name"], ou["Id"])
            node["children"].append(walk(ou["Id"], ou["Name"], "OU", path + [ou["Name"]]))
        return node

    roots = list(_paginate(org, "list_roots", "Roots"))
    if not roots:
        raise RuntimeError("No organization root found")
    root = roots[0]
    log.info("Reading org structure from root %s", root["Id"])
    tree = walk(root["Id"], root.get("Name", "Root"), "ROOT", [])
    # the root ARN embeds the org ID (arn:aws:organizations::<mgmt>:root/o-xxxx/r-xxxx), so no
    # extra permission is needed; DescribeOrganization is only a fallback
    m = re.search(r":root/(o-[a-z0-9]+)/", root.get("Arn", ""))
    tree["orgId"] = m.group(1) if m else None
    if not tree["orgId"]:
        try:
            tree["orgId"] = org.describe_organization()["Organization"]["Id"]
        except ClientError as e:
            log.warning("Can't read organization ID (%s); omitting it", e.response["Error"]["Code"])
    log.info("Found %d accounts (%d inactive skipped)", len(accounts), skipped)
    return {"tree": tree, "accounts": accounts}


# --------------------------------------------------------------------------- #
# DynamoDB metadata
# --------------------------------------------------------------------------- #
def _caller_account(session: boto3.Session) -> str:
    return get_client("sts", session).get_caller_identity()["Account"]


def get_ddb_session(cfg: Dict[str, Any], org_session: boto3.Session) -> boto3.Session:
    """Session in the table's account: ddb_profile, else the org session (same account) or an
    assumed ddb_role_name. Refuses credentials for any other account."""
    account = cfg["ddb_account_id"]
    if cfg["ddb_profile"]:
        log.debug("DDB via profile %s", cfg["ddb_profile"])
        session = get_session(cfg["ddb_profile"], cfg["ddb_region"])
    elif _caller_account(org_session) == account:
        log.debug("DDB via the Organizations session")
        session = org_session
    else:
        partition = org_session.get_partition_for_region(cfg["ddb_region"])
        role_arn = f"arn:{partition}:iam::{account}:role/{cfg['ddb_role_name']}"
        log.debug("DDB via assume-role %s", role_arn)
        creds = get_client("sts", org_session).assume_role(
            RoleArn=role_arn, RoleSessionName="aws-bookmarks-generator"
        )["Credentials"]
        session = boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=cfg["ddb_region"],
        )
    actual = _caller_account(session)
    if actual != account:
        raise RuntimeError(f"DynamoDB credentials are for account {actual}, but the table is in {account}; "
                           "fix --ddb-profile / BM_DDB_PROFILE")
    return session


def fetch_metadata(ddb, table: str, attrs: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
    """Scan the table -> {account_id: {shortName, ou, envRaw}}."""
    names = {f"#a{i}": a for i, a in enumerate(attrs.values())}
    deser = TypeDeserializer()
    out: Dict[str, Dict[str, Any]] = {}
    log.info("Scanning DynamoDB table %s", table)
    for item in _paginate(
        ddb,
        "scan",
        "Items",
        TableName=table,
        ProjectionExpression=", ".join(names),
        ExpressionAttributeNames=names,
    ):
        row = {k: deser.deserialize(v) for k, v in item.items()}
        acct_id = row.get(attrs["account_id"])
        if acct_id in (None, ""):
            continue
        acct_id = normalize_account_id(acct_id)
        if acct_id in out:
            log.warning("Account %s appears in more than one table row; using the last one", acct_id)
        out[acct_id] = {
            "shortName": row.get(attrs["short_name"]),
            "ou": row.get(attrs["ou"]),
            "envRaw": row.get(attrs["environment"]),
        }
    log.info("Loaded metadata for %d accounts", len(out))
    return out


# --------------------------------------------------------------------------- #
# Merge + render
# --------------------------------------------------------------------------- #
def _blank(value: Any) -> bool:
    return value is None or not str(value).strip()


def merge(org: Dict[str, Any], meta: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged = []
    for acct_id, info in org["accounts"].items():
        m = meta.get(acct_id, {})
        ou = (str(m["ou"]).strip() if m.get("ou") not in (None, "") else "")
        env_raw = m.get("envRaw")
        env = normalize_env(env_raw)
        if env_raw not in (None, "") and env is None:
            log.warning("Account %s: unrecognised environment %r", acct_id, env_raw)
        merged.append(
            {
                **info,
                # short codes are lowercase by convention; the account-name fallback keeps its case
                "shortName": str(m["shortName"]).strip().lower() if not _blank(m.get("shortName")) else info["name"],
                "domain": ou or LEGACY_DOMAIN,
                "env": env,
                "envRaw": None if env_raw is None else str(env_raw),
                "hasMetadata": bool(m),
            }
        )
    orphans = set(meta) - set(org["accounts"])
    if orphans:
        log.warning("%d DDB entries match no active org account: %s", len(orphans), ", ".join(sorted(orphans)[:10]))
    missing = sum(1 for a in merged if not a["hasMetadata"])
    if missing:
        log.warning("%d org accounts have no DDB metadata (shown as %s)", missing, LEGACY_DOMAIN)
    return sorted(merged, key=lambda a: a["shortName"].lower())


def render(data: Dict[str, Any]) -> str:
    payload = json.dumps(data, separators=(",", ":"), default=str).replace("<", "\\u003c")
    return HTML_TEMPLATE.replace("__TITLE__", _html_escape(data["title"])).replace("/*__DATA__*/null", payload)


def _html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def render_path(output_dir: Path, now: datetime, stem: str = "aws-bookmarks") -> Path:
    """output_dir/<stem>-<UTC timestamp>.html, never overwriting an existing render."""
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{stem}-{now.strftime('%Y%m%dT%H%M%SZ')}"
    out = output_dir / f"{stem}.html"
    n = 1
    while out.exists():
        out = output_dir / f"{stem}-{n}.html"
        n += 1
    return out


def env_file_label(path: Path) -> str:
    """Default org label from an env file name: prod.env, .env.prod and prod/.env all give "prod"."""
    name = path.name
    if name.startswith(".env."):
        name = name[5:]
    elif name.endswith(".env"):
        name = name[:-4]
    return name.strip(".") or path.resolve().parent.name


def fetch_org_data(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """One org's tree and merged accounts: {"orgId", "signinUrl", "tree", "accounts"}."""
    log.debug("Using profile=%s region=%s", cfg["profile"] or "default", cfg["region"])
    org_session = get_session(cfg["profile"], cfg["region"])
    org = fetch_org(get_client("organizations", org_session), cfg["include_inactive"])

    ddb_session = get_ddb_session(cfg, org_session)
    meta = fetch_metadata(
        get_client("dynamodb", ddb_session, cfg["ddb_region"]),
        cfg["ddb_table"],
        {
            "account_id": cfg["attr_account_id"],
            "short_name": cfg["attr_short_name"],
            "ou": cfg["attr_ou"],
            "environment": cfg["attr_environment"],
        },
    )
    partition = org_session.get_partition_for_region(cfg["region"])
    return {
        "orgId": org["tree"].get("orgId"),
        "signinUrl": SIGNIN_URLS.get(partition, SIGNIN_URLS["aws"]),
        "tree": org["tree"],
        "accounts": merge(org, meta),
    }


def _generate(env_files: Optional[Sequence[Union[str, Path]]], kwargs: Dict[str, Any]) -> Tuple[Path, List[str]]:
    """Returns (path written, labels of orgs that failed and were left out)."""
    if isinstance(env_files, (str, Path)):
        env_files = [env_files]
    if env_files:
        paths = [Path(f) for f in env_files]
        for path in paths:
            if not path.is_file():
                raise ValueError(f"Env file not found: {path}")
        envs: List[Optional[Dict[str, str]]] = [read_dotenv(path) for path in paths]
    else:
        load_dotenv()
        paths, envs = [], [None]
    multi = len(envs) > 1
    known = {env_var for env_var, _ in SETTINGS.values()}
    for path, env in zip(paths, envs):
        extra = sorted(set(env or {}) - known)
        if extra and multi:
            # other variables (AWS credentials, proxies, ...) are process-wide, so they can't differ per org
            log.warning("%s: ignoring %s (only bookmark settings are read per org; export the rest in the shell)",
                        path, ", ".join(extra))
        elif extra:
            for key in extra:
                os.environ.setdefault(key, env[key])
    if multi:
        per_org = sorted({k for k, v in kwargs.items() if v is not None} - PAGE_SETTINGS)
        if per_org:
            raise ValueError(f"With several env files, set {', '.join(per_org)} in each file, not as a flag/kwarg")
    cfgs = [resolve_settings(kwargs, env) for env in envs]
    if multi:
        for cfg, path in zip(cfgs, paths):
            cfg["org_label"] = cfg["org_label"] or env_file_label(path)
        labels = [cfg["org_label"] for cfg in cfgs]
        bad = sorted({label for label in labels if ":" in label})
        if bad:
            raise ValueError(f"Org label(s) {', '.join(bad)} contain ':'; set BM_ORG_LABEL without one")
        dupes = sorted({label for label in labels if labels.count(label) > 1})
        if dupes:
            raise ValueError(f"Duplicate org label(s) {', '.join(dupes)}; set BM_ORG_LABEL in each env file")

    orgs: List[Dict[str, Any]] = []
    accounts: List[Dict[str, Any]] = []
    failed: List[str] = []
    for cfg in cfgs:
        label = cfg["org_label"]
        if multi:
            log.info("Org %s", label)
        if cfg["omit_global_roles"] and not cfg["org_roles"]:
            log.warning("Org %s omits the global roles but has no org roles; its accounts get no links",
                        label or "(default)")
        try:
            data = fetch_org_data(cfg)
        except Exception as e:  # one bad org mustn't sink the others
            if not multi:
                raise
            detail = e.response["Error"]["Code"] if isinstance(e, ClientError) else f"{type(e).__name__}: {e}"
            log.error("Org %s left out of the page: %s", label, detail)
            failed.append(label)
            continue
        # an account belongs to one org, so shared accounts mean the same org (covers unknown org IDs)
        ids = {a["id"] for a in data["accounts"]}
        same = next((o for o in orgs if (data["orgId"] and o["orgId"] == data["orgId"])
                     or ((not data["orgId"] or not o["orgId"]) and ids & o["accountIds"])), None)
        if same:
            raise ValueError(f"Orgs {same['label']} and {label} are the same organization "
                             f"({data['orgId'] or 'shared accounts'}); check AWS_PROFILE in their env files")
        index = len(orgs)
        orgs.append({"label": label, "orgId": data["orgId"], "signinUrl": data["signinUrl"], "tree": data["tree"],
                     "roles": cfg["org_roles"], "omitGlobalRoles": cfg["omit_global_roles"], "accountIds": ids})
        for acct in data["accounts"]:
            acct["org"] = index
        accounts.extend(data["accounts"])
    if not orgs:
        raise RuntimeError("No org could be read")

    # page settings: title and output dir from the first org's settings, roles merged in order
    for o in orgs:
        del o["accountIds"]
    page = cfgs[0]
    roles: List[str] = []
    for cfg in cfgs:
        if multi and cfg["omit_global_roles"]:
            log.debug("Org %s omits the global roles; its BM_DEFAULT_ROLES aren't added to them", cfg["org_label"])
            continue
        roles += [r for r in cfg["roles"] if r not in roles]
    now = datetime.now(timezone.utc)
    data = {
        "title": page["title"],
        "generatedAt": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "environments": list(ENVIRONMENTS),
        "legacyDomain": LEGACY_DOMAIN,
        "defaultRoles": roles,
        "multi": multi,
        "orgs": orgs,
        "failedOrgs": failed,
        "accounts": sorted(accounts, key=lambda a: a["shortName"].lower()),
    }
    out = render_path(Path(page["output_dir"]), now, "aws-bookmarks-all" if multi else "aws-bookmarks")
    out.write_text(render(data), encoding="utf-8")
    log.info("Wrote %s (%d accounts%s)", out, len(accounts), f", {len(orgs)} orgs" if multi else "")
    return out, failed


def generate(env_files: Optional[Sequence[Union[str, Path]]] = None, **kwargs: Any) -> Path:
    """Build the bookmarks page. Accepts any key in SETTINGS as a kwarg.

    env_files: one env file per org. None/one file -> single-org page; two or more -> one
    combined page. With several files only page settings (roles, title, output_dir) may be
    passed as kwargs; an org that fails is logged and left out.
    """
    return _generate(env_files, kwargs)[0]


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Generate an AWS switch-role bookmarks page.")
    p.add_argument("--env-file", action="append", metavar="PATH",
                   help="Env file for one org (default ./.env); repeat for a combined multi-org page")
    p.add_argument("--profile", help="Profile for the Organizations (management/delegated admin) account")
    p.add_argument("--region", help="Region (default us-east-1)")
    p.add_argument("--ddb-table-arn", help="ARN of the account metadata table (default BM_DDB_TABLE_ARN)")
    p.add_argument("--ddb-profile", help="Profile for the table's account (default: --profile, or assume --ddb-role-name)")
    p.add_argument("--ddb-role-name", help="Role assumed in the table's account when --ddb-profile isn't set")
    p.add_argument("--attr-account-id", help="DDB attribute: account number (default accountNumber)")
    p.add_argument("--attr-short-name", help="DDB attribute: short name (default accountShortCode)")
    p.add_argument("--attr-ou", help="DDB attribute: OU / business domain (default ou)")
    p.add_argument("--attr-environment", help="DDB attribute: environment (default environment)")
    p.add_argument("--roles", help="Comma-separated roles seeded into the page on first load")
    p.add_argument("--org-roles", help="Comma-separated roles added for this org only (default BM_ORG_ROLES)")
    p.add_argument("--omit-global-roles", action="store_const", const=True,
                   help="Show only --org-roles for this org, not the global roles")
    p.add_argument("--title", help="Page title")
    p.add_argument("-o", "--output-dir", help="Directory for timestamped renders (default output)")
    p.add_argument("--include-inactive", action="store_const", const=True, help="Include suspended/closed accounts")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("-q", "--quiet", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    kwargs = {k: v for k, v in vars(args).items() if k in SETTINGS and v is not None}
    try:
        out, failed = _generate(args.env_file, kwargs)
    except ClientError as e:
        log.error("AWS error %s: %s", e.response["Error"]["Code"], e.response["Error"]["Message"])
        return 1
    except (ValueError, RuntimeError, BotoCoreError) as e:
        log.error("%s", e)
        return 2
    print(out.resolve())
    return 1 if failed else 0


# --------------------------------------------------------------------------- #
# Page template (self-contained: no external assets)
# --------------------------------------------------------------------------- #
HTML_TEMPLATE = r"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<script>try{var t=JSON.parse(localStorage.getItem('awsbm.theme'));if(t)document.documentElement.dataset.theme=t;}catch(e){}</script>
<style>
:root{
  --bg:#0f1216;--panel:#161b22;--card:#1c222b;--card-h:#232a35;--line:#2a313c;
  --text:#e6e9ee;--muted:#8b95a5;--accent:#ff9900;--accent-t:#111;--focus:#58a6ff;
  --lab:#7aa7e0;--sbx:#8fbf7a;--dev:#e6c15c;--tst:#f0995a;--prd:#ef6b5f;--bil:#b99af0;--none:#6b7482;
  --shadow:0 1px 2px rgba(0,0,0,.4);color-scheme:dark;
}
[data-theme="light"]{
  --bg:#f3f5f8;--panel:#ffffff;--card:#ffffff;--card-h:#f7f9fc;--line:#dde2ea;
  --text:#1b2330;--muted:#5d6878;--accent:#d97e00;--accent-t:#fff;--focus:#0b62d6;
  --lab:#2f6db5;--sbx:#3f8a2c;--dev:#9a7400;--tst:#c05a14;--prd:#c8322a;--bil:#7446c9;--none:#7b8492;
  --shadow:0 1px 2px rgba(20,30,50,.08);color-scheme:light;
}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--text);font:13px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  display:flex;flex-direction:column;height:100vh;height:100dvh;overflow:hidden}
button,input{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--focus);outline-offset:1px}
header{display:flex;flex-wrap:wrap;align-items:center;gap:10px 14px;padding:10px 16px;background:var(--panel);border-bottom:1px solid var(--line);flex:none}
.brand{display:flex;flex-direction:column;margin-right:auto;min-width:0}
.brand h1{font-size:15px;margin:0;white-space:nowrap}
.brand small{color:var(--muted);font-size:11px}
.search{flex:1 1 220px;max-width:360px;padding:6px 10px;border-radius:6px;border:1px solid var(--line);background:var(--bg)}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:6px;overflow:hidden}
.seg button{border:0;background:transparent;padding:5px 10px;cursor:pointer;color:var(--muted)}
.seg button[aria-pressed="true"]{background:var(--accent);color:var(--accent-t)}
.envs{display:flex;gap:4px;flex-wrap:wrap}
.chip{border:1px solid var(--line);background:transparent;border-radius:999px;padding:3px 9px;cursor:pointer;display:inline-flex;gap:5px;align-items:center;color:var(--muted)}
.chip .dot{width:8px;height:8px;border-radius:50%;background:var(--c)}
.chip[aria-pressed="true"]{border-color:var(--c);color:var(--text);background:color-mix(in srgb,var(--c) 18%,transparent)}
.chip b{font-weight:500;color:var(--muted);font-size:11px}
.btn{border:1px solid var(--line);background:var(--card);border-radius:6px;padding:5px 10px;cursor:pointer}
.btn:hover{background:var(--card-h)}
.btn b{font-weight:600;color:var(--muted);margin-left:2px}
dialog{border:1px solid var(--line);border-radius:10px;background:var(--panel);color:var(--text);padding:0;
  width:min(420px,calc(100vw - 32px));max-height:min(80vh,600px);box-shadow:0 12px 40px rgba(0,0,0,.45)}
dialog::backdrop{background:rgba(0,0,0,.55)}
.dlg{display:flex;flex-direction:column;gap:12px;padding:16px;max-height:min(80vh,600px)}
.dlg-head{display:flex;align-items:center;gap:8px}
.dlg-head h2{font-size:15px;margin:0;flex:1}
.dlg-head button{border:0;background:transparent;color:var(--muted);font-size:20px;line-height:1;cursor:pointer;padding:0 4px}
.dlg-head button:hover{color:var(--text)}
.roles{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:4px;overflow:auto;min-height:0}
.roles:empty::after{content:"No roles yet. Add one below.";color:var(--muted);font-size:12px}
.roles li{display:flex;align-items:center;gap:6px;padding:6px 8px;cursor:grab;border-radius:5px;background:var(--card);border:1px solid var(--line)}
.roles li span{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.roles li button{border:0;background:transparent;cursor:pointer;color:var(--muted);padding:0 2px;font-size:14px;line-height:1}
.roles li button:hover{color:var(--prd)}
.roles li.drag{opacity:.4}
.addrole{display:flex;gap:6px}
.addrole input{flex:1;min-width:0;padding:5px 8px;border-radius:5px;border:1px solid var(--line);background:var(--bg)}
.hint{color:var(--muted);font-size:11px;margin:0}
main{flex:1;overflow:auto;padding:12px 16px 24px;min-height:0}
.icon-btn{border:0;background:transparent;color:var(--muted);opacity:.55;padding:4px;border-radius:6px;cursor:pointer;display:inline-flex}
.icon-btn:hover,.icon-btn:focus-visible{opacity:1;color:var(--text);background:var(--card)}
.icon-btn svg{width:16px;height:16px}
.adv-item{display:flex;align-items:center;gap:12px;justify-content:space-between}
.adv-item p{margin:0;font-size:12px;color:var(--muted)}
.linkish{border:0;background:none;padding:0;color:var(--accent);cursor:pointer;font-size:11.5px}
.empty{color:var(--muted);padding:40px;text-align:center}
details.group{margin-bottom:10px;border:1px solid var(--line);border-radius:8px;background:var(--panel)}
details.group>summary{list-style:none;cursor:pointer;padding:8px 12px;display:flex;align-items:center;gap:8px;font-weight:600;user-select:none}
details.group>summary::-webkit-details-marker{display:none}
details.group>summary::before{content:"▸";color:var(--muted);transition:transform .15s}
details.group[open]>summary::before{transform:rotate(90deg)}
summary .count{color:var(--muted);font-weight:400;font-size:12px}
summary .ouid{font:400 11px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);border:0;background:none;padding:1px 4px;border-radius:4px;cursor:copy}
summary .ouid:hover{color:var(--text);background:var(--card)}
summary .mix{display:flex;gap:3px;margin-left:auto}
summary .mix i{font-style:normal;font-size:10px;padding:0 5px;border-radius:3px;color:var(--c);border:1px solid color-mix(in srgb,var(--c) 50%,transparent)}
.gbody{padding:0 12px 12px}
.gbody details.group{background:var(--bg);margin:8px 0 0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:8px}
.card{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--c);border-radius:6px;padding:8px 10px;box-shadow:var(--shadow);display:flex;flex-direction:column;gap:6px;min-width:0}
.card:hover{background:var(--card-h)}
.ctop{display:flex;align-items:baseline;gap:8px;min-width:0}
.ctop strong{font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.badge{margin-left:auto;font-size:10.5px;font-weight:600;padding:1px 6px;border-radius:4px;color:var(--c);background:color-mix(in srgb,var(--c) 16%,transparent);flex:none}
.cmeta{display:flex;gap:8px;color:var(--muted);font-size:11.5px;min-width:0}
.cmeta .name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.acct{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;cursor:pointer;border:0;background:none;padding:0;color:var(--muted);font-size:11.5px}
.acct:hover{color:var(--text)}
.links{display:flex;flex-wrap:wrap;gap:4px}
.links a{font-size:11.5px;text-decoration:none;color:var(--text);border:1px solid var(--line);border-radius:4px;padding:2px 7px;background:var(--bg)}
.links a:hover{border-color:var(--accent);color:var(--accent)}
.links .none{color:var(--muted);font-size:11.5px}
.toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:var(--text);color:var(--bg);padding:6px 12px;border-radius:6px;opacity:0;transition:opacity .2s;pointer-events:none}
.toast.show{opacity:1}
@media (max-width:760px){
  .search{max-width:none}
}
</style>
</head>
<body>
<header>
  <div class="brand"><h1 id="title"></h1><small id="sub"></small></div>
  <input id="q" class="search" type="search" placeholder="Search name, ID, domain, OU…  ( / )" autocomplete="off">
  <div class="seg" role="group" aria-label="Group by">
    <button data-view="domain">Business Domains</button><button data-view="org">Org Hierarchy</button>
  </div>
  <div class="envs" id="orgs" role="group" aria-label="Organizations" hidden></div>
  <div class="envs" id="envs" role="group" aria-label="Environment filter"></div>
  <button class="btn" id="rolesBtn" title="Manage roles" aria-haspopup="dialog">Roles <b id="roleCount"></b></button>
  <button class="btn" id="collapse" title="Collapse / expand all">⇕</button>
  <button class="btn" id="theme" title="Toggle light/dark"></button>
  <button class="icon-btn" id="advBtn" title="Advanced" aria-label="Advanced" aria-haspopup="dialog"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg></button>
</header>
<main id="main"></main>
<dialog id="rolesDlg" aria-labelledby="rolesTitle">
  <div class="dlg">
    <div class="dlg-head"><h2 id="rolesTitle">Roles</h2><button class="dlg-close" aria-label="Close">×</button></div>
    <ul class="roles" id="roles"></ul>
    <form class="addrole" id="addrole"><input id="newrole" placeholder="Role name or role ARN" aria-label="Role name"><button class="btn">Add</button></form>
    <p class="hint">Saved in this browser. Each account shows a switch-role link per role, in this order (drag to reorder). Defaults added to the config later appear automatically; roles you remove stay removed.</p>
    <p class="hint" id="orgRoles" hidden></p>
  </div>
</dialog>
<dialog id="advDlg" aria-labelledby="advTitle">
  <div class="dlg">
    <div class="dlg-head"><h2 id="advTitle">Advanced</h2><button class="dlg-close" aria-label="Close">×</button></div>
    <div class="adv-item">
      <p>Download the accounts currently shown (search and filters applied) as a bookmarks file, in folders matching the current view. Import it from your browser's bookmarks menu.</p>
      <button class="btn" id="export">Export bookmarks</button>
    </div>
  </div>
</dialog>
<div class="toast" id="toast"></div>
<script>
"use strict";
const DATA = /*__DATA__*/null;
const ENV_ORDER = DATA.environments;
const ENV_VAR = {Lab:"--lab",Sbx:"--sbx",Dev:"--dev",Tst:"--tst",Prd:"--prd",Bil:"--bil"};
// Console header colours accepted by the switchrole `color` parameter (Bil: default header)
const ENV_CONSOLE = {Lab:"99BCE3",Sbx:"B7CA9D",Dev:"FAD791",Tst:"FBBF93",Prd:"F2B0A9"};
const byId = Object.fromEntries(DATA.accounts.map(a => [a.id, a]));
// several orgs: each is a top-level group (a.org indexes DATA.orgs)
const MULTI = DATA.multi;

const LS = {
  get(k, d) { try { const v = localStorage.getItem("awsbm." + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem("awsbm." + k, JSON.stringify(v)); } catch (e) {} },
};
// Configured default roles seed a new browser's list. Defaults added to the config later are
// appended once; roles the user removed stay removed (tracked in awsbm.seenDefaults).
function initRoles() {
  const saved = LS.get("roles", null);
  const seen = new Set(LS.get("seenDefaults", saved ? DATA.defaultRoles : []));
  const roles = saved || [];
  for (const r of DATA.defaultRoles) if (!seen.has(r) && !roles.includes(r)) roles.push(r);
  LS.set("roles", roles);
  LS.set("seenDefaults", [...new Set([...seen, ...DATA.defaultRoles])]);
  return roles;
}
const state = {
  view: LS.get("view", "domain"),
  theme: LS.get("theme", "dark"),
  roles: initRoles(),
  envs: new Set(LS.get("envs", [])),
  collapsed: new Set(LS.get("collapsed", [])),
  hiddenOrgs: new Set(LS.get("hiddenOrgs", [])),  // org labels switched off in this browser
  q: "",
};

function el(tag, attrs, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "style") n.style.cssText = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat(Infinity)) if (c != null) n.append(c.nodeType ? c : document.createTextNode(String(c)));
  return n;
}
const envColor = env => `var(${ENV_VAR[env] || "--none"})`;
const envRank = env => { const i = ENV_ORDER.indexOf(env); return i < 0 ? 99 : i; };
const byEnvThenName = (a, b) => envRank(a.env) - envRank(b.env) || a.shortName.localeCompare(b.shortName);

function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.classList.add("show");
  clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.remove("show"), 1400);
}

/* ---------- switch-role links ---------- */
// global roles (this browser's list) plus the org's configured roles; an org can omit the global list
function rolesFor(a) {
  const o = DATA.orgs[a.org];
  return o.omitGlobalRoles ? o.roles : [...state.roles, ...o.roles.filter(r => !state.roles.includes(r))];
}
function displayName(a, role) {
  return `${a.shortName} / ${role}`.slice(0, 64);
}
function switchUrl(a, role) {
  const p = new URLSearchParams({account: a.id, roleName: role, displayName: displayName(a, role)});
  if (ENV_CONSOLE[a.env]) p.set("color", ENV_CONSOLE[a.env]);
  return `${DATA.orgs[a.org].signinUrl}/switchrole?${String(p).replace(/\+/g, "%20")}`;
}

/* ---------- filtering ---------- */
const orgOn = a => !MULTI || !state.hiddenOrgs.has(DATA.orgs[a.org].label);
function visible(a) {
  if (!orgOn(a)) return false;
  if (state.envs.size && !state.envs.has(a.env || "—")) return false;
  if (!state.q) return true;
  const hay = [a.shortName, a.name, a.id, a.domain, a.env, a.envRaw, a.email, ...a.orgPath,
    MULTI ? DATA.orgs[a.org].label : ""].join(" ").toLowerCase();
  return state.q.split(/\s+/).every(t => hay.includes(t));
}

/* ---------- rendering ---------- */
function card(a) {
  const roles = rolesFor(a);
  const links = roles.length
    ? roles.map(r => el("a", {href: switchUrl(a, r), target: "_blank", rel: "noopener", title: `Switch to ${r} in ${a.id}`}, r))
    : DATA.orgs[a.org].omitGlobalRoles ? [el("span", {class: "none"}, "No roles configured for this org")]
    : [el("button", {class: "linkish", onclick: openRoles}, "Add a role to get links")];
  return el("div", {class: "card", style: `--c:${envColor(a.env)}`},
    el("div", {class: "ctop"},
      el("strong", {title: a.shortName}, a.shortName),
      el("span", {class: "badge", title: a.envRaw || "No environment"}, a.env || "—")),
    el("div", {class: "cmeta"},
      el("span", {class: "name", title: `${a.name}\n${a.email}\n${a.orgPath.join(" / ") || "Root"}`}, a.name),
      el("button", {class: "acct", title: "Copy account ID", onclick: () => copy(a.id)}, a.id)),
    el("div", {class: "links"}, links));
}
function envMix(list) {
  const counts = {};
  for (const a of list) counts[a.env || "—"] = (counts[a.env || "—"] || 0) + 1;
  return el("span", {class: "mix"}, [...ENV_ORDER, "—"].filter(e => counts[e])
    .map(e => el("i", {style: `--c:${envColor(e)}`, title: `${counts[e]} ${e}`}, `${e} ${counts[e]}`)));
}
// Copy buttons sit inside <summary>; some browsers toggle the group on their click even when
// it's cancelled, so a toggle right after an ID click is undone.
let idClickAt = 0;
const fromIdClick = e => { if (e.target.closest && e.target.closest(".ouid")) { e.preventDefault(); idClickAt = Date.now(); } };
// copy buttons inside <summary> must not toggle the <details>
const idBtn = (id, what) => el("button", {class: "ouid", title: `Copy ${what}`,
  onclick: e => { e.preventDefault(); idClickAt = Date.now(); copy(id); }}, id);
const orgIdBtn = o => o.orgId ? idBtn(o.orgId, "organization ID") : null;
// visible accounts of each org -> fn(org, accts), skipping orgs with none
const perOrg = fn => DATA.orgs.map((o, i) => {
  const accts = DATA.accounts.filter(a => a.org === i && visible(a));
  return accts.length ? fn(o, accts) : null;
}).filter(Boolean);
function group(key, label, accts, children) {
  const d = el("details", {class: "group", open: !state.collapsed.has(key)},
    el("summary", {onclick: fromIdClick}, label, el("span", {class: "count"}, `${accts.length}`), envMix(accts)),
    el("div", {class: "gbody"},
      accts.length && children == null ? el("div", {class: "grid"}, accts.map(card)) : null,
      children));
  d.addEventListener("toggle", () => {
    if (Date.now() - idClickAt < 500) {
      const want = !state.collapsed.has(key);
      if (d.open !== want) d.open = want;
      return;
    }
    if (d.open !== state.collapsed.has(key)) return;
    d.open ? state.collapsed.delete(key) : state.collapsed.add(key);
    LS.set("collapsed", [...state.collapsed]);
  });
  return d;
}
function domainGroups(accts) {
  const groups = new Map();
  for (const a of accts) {
    if (!groups.has(a.domain)) groups.set(a.domain, []);
    groups.get(a.domain).push(a);
  }
  return [...groups.keys()]
    .sort((x, y) => (x === DATA.legacyDomain) - (y === DATA.legacyDomain) || x.localeCompare(y))
    .map(d => [d, groups.get(d).sort(byEnvThenName)]);
}
function renderDomain() {
  const domains = (accts, prefix) => domainGroups(accts).map(([d, list]) => group(prefix + d, d, list));
  if (!MULTI) return domains(DATA.accounts.filter(visible), "d:");
  return perOrg((o, accts) => group("g:" + o.label, [o.label, orgIdBtn(o)], accts, domains(accts, `d:${o.label}:`)));
}
function renderOrg() {
  function build(node, o) {
    const own = node.accounts.map(id => byId[id]).filter(a => a && visible(a)).sort(byEnvThenName);
    const kids = node.children.map(c => build(c, o)).filter(Boolean);
    const all = own.concat(...kids.map(k => k.all));
    if (!all.length) return null;
    const body = [own.length ? el("div", {class: "grid"}, own.map(card)) : null, ...kids.map(k => k.node)];
    // with several orgs the root group is the org's top-level group
    const label = node.type === "ROOT"
      ? [MULTI ? o.label : node.name, orgIdBtn(o), idBtn(node.id, "root ID")]
      : [node.name, idBtn(node.id, "OU ID")];
    return {all, node: group(MULTI ? `o:${o.label}:${node.id}` : "o:" + node.id, label, all, body)};
  }
  return DATA.orgs.map(o => build(o.tree, o)).filter(Boolean).map(r => r.node);
}
function render() {
  const main = document.getElementById("main");
  const scroll = main.scrollTop;
  const nodes = state.view === "org" ? renderOrg() : renderDomain();
  main.replaceChildren(...(nodes.length ? nodes : [el("div", {class: "empty"}, "No accounts match.")]));
  main.scrollTop = scroll;
  const shown = DATA.accounts.filter(visible).length;
  document.getElementById("sub").textContent =
    `${shown} of ${DATA.accounts.length} accounts` + (MULTI ? ` in ${DATA.orgs.length} org${DATA.orgs.length === 1 ? "" : "s"}` : "") +
    ` · generated ${DATA.generatedAt}` + (DATA.failedOrgs.length ? ` · not read: ${DATA.failedOrgs.join(", ")}` : "");
}
function renderEnvs() {
  const counts = {};
  for (const a of DATA.accounts) if (orgOn(a)) counts[a.env || "—"] = (counts[a.env || "—"] || 0) + 1;
  const box = document.getElementById("envs");
  box.replaceChildren(...[...ENV_ORDER, "—"].filter(e => counts[e] || ENV_ORDER.includes(e) || state.envs.has(e)).map(e =>
    el("button", {class: "chip", style: `--c:${envColor(e)}`, "aria-pressed": String(state.envs.has(e)),
      title: e === "—" ? "No environment set" : `Show only ${e} (multi-select)`,
      onclick: () => { state.envs.has(e) ? state.envs.delete(e) : state.envs.add(e); LS.set("envs", [...state.envs]); renderEnvs(); render(); }},
      el("span", {class: "dot"}), e, el("b", {}, counts[e] || 0))));
}
// combined page: switch orgs on/off; remembered per browser (e.g. one browser profile per org)
function renderOrgs() {
  const box = document.getElementById("orgs");
  box.hidden = !MULTI;
  if (!MULTI) return;
  box.replaceChildren(...DATA.orgs.map((o, i) => el("button", {class: "chip", style: "--c:var(--accent)",
      "aria-pressed": String(!state.hiddenOrgs.has(o.label)), title: `Show or hide ${o.label} (remembered in this browser)`,
      onclick: () => {
        state.hiddenOrgs.has(o.label) ? state.hiddenOrgs.delete(o.label) : state.hiddenOrgs.add(o.label);
        LS.set("hiddenOrgs", [...state.hiddenOrgs]); renderOrgs(); renderEnvs(); render();
      }},
    el("span", {class: "dot"}), o.label, el("b", {}, DATA.accounts.filter(a => a.org === i).length))));
}
function renderRoles() {
  const ul = document.getElementById("roles");
  let dragFrom = null;
  ul.replaceChildren(...state.roles.map((r, i) => el("li", {draggable: "true",
      ondragstart: e => { dragFrom = i; e.dataTransfer.setData("text/plain", r); e.dataTransfer.effectAllowed = "move"; e.currentTarget.classList.add("drag"); },
      ondragend: e => e.currentTarget.classList.remove("drag"),
      ondragover: e => e.preventDefault(),
      ondrop: e => { e.preventDefault(); if (dragFrom === null || dragFrom === i) return;
        const [m] = state.roles.splice(dragFrom, 1); state.roles.splice(i, 0, m); saveRoles(); }},
    el("span", {title: r}, r),
    el("button", {title: `Remove ${r}`, "aria-label": `Remove ${r}`, onclick: () => { state.roles.splice(i, 1); saveRoles(); }}, "×"))));
  document.getElementById("roleCount").textContent = state.roles.length;
}
// roles set per org in the config (read-only here)
function renderOrgRoles() {
  const lines = DATA.orgs.filter(o => o.roles.length || o.omitGlobalRoles).map(o =>
    `${MULTI ? o.label : "This org"}: ${o.roles.join(", ") || "no roles"} ` +
    (o.omitGlobalRoles ? "(only these; the list above doesn't apply)" : "(added to the list above)"));
  const p = document.getElementById("orgRoles");
  p.hidden = !lines.length;
  p.replaceChildren(...lines.flatMap((l, i) => i ? [el("br"), l] : [l]));
}
function openRoles() {
  const dlg = document.getElementById("rolesDlg");
  if (!dlg.open) dlg.showModal();
  document.getElementById("newrole").focus();
}
function saveRoles() { LS.set("roles", state.roles); renderRoles(); render(); }
function applyView() {
  for (const b of document.querySelectorAll("[data-view]")) b.setAttribute("aria-pressed", String(b.dataset.view === state.view));
}
function applyTheme() {
  document.documentElement.dataset.theme = state.theme;
  document.getElementById("theme").textContent = state.theme === "dark" ? "☀ Light" : "☾ Dark";
}
async function copy(text) {
  try { await navigator.clipboard.writeText(text); toast(`Copied ${text}`); }
  catch (e) { toast(text); }
}

/* ---------- bookmark export (Netscape format, importable by all browsers) ---------- */
function exportBookmarks() {
  const linked = a => visible(a) && rolesFor(a).length;
  if (!DATA.accounts.some(linked)) { toast(state.roles.length ? "No shown account has roles" : "Add a role first"); return; }
  const esc = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const ts = Math.floor(Date.now() / 1000);
  const links = (accts, pad) => accts.flatMap(a => rolesFor(a).map(r =>
    `${pad}<DT><A HREF="${esc(switchUrl(a, r))}" ADD_DATE="${ts}">${esc(`${a.shortName} (${a.env || "—"}) · ${r}`)}</A>`));
  const folder = (name, inner, pad) => [`${pad}<DT><H3 ADD_DATE="${ts}">${esc(name)}</H3>`, `${pad}<DL><p>`, ...inner, `${pad}</DL><p>`];
  let body = [];
  const domainFolders = (accts, pad) => domainGroups(accts).flatMap(([d, list]) => folder(d, links(list, pad + "    "), pad));
  if (state.view === "org") {
    const walk = (node, pad, name) => {
      const own = node.accounts.map(id => byId[id]).filter(a => a && linked(a)).sort(byEnvThenName);
      const kids = node.children.map(c => walk(c, pad + "    ", c.name)).filter(k => k.length);
      if (!own.length && !kids.length) return [];
      return folder(name, [...links(own, pad + "    "), ...kids.flat()], pad);
    };
    body = DATA.orgs.flatMap(o => walk(o.tree, "    ", MULTI ? o.label : o.tree.name));
  } else if (MULTI) {
    body = perOrg((o, accts) => accts.some(linked) ? folder(o.label, domainFolders(accts.filter(linked), "        "), "    ") : null)
      .filter(Boolean).flat();
  } else {
    body = domainFolders(DATA.accounts.filter(linked), "    ");
  }
  const html = ["<!DOCTYPE NETSCAPE-Bookmark-file-1>",
    '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
    "<TITLE>Bookmarks</TITLE>", "<H1>Bookmarks</H1>", "<DL><p>",
    ...folder(DATA.title, body, ""), "</DL><p>"].join("\n");
  const url = URL.createObjectURL(new Blob([html], {type: "text/html"}));
  el("a", {href: url, download: `aws-bookmarks-${state.view}.html`}).click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/* ---------- wire up ---------- */
document.title = DATA.title;
document.getElementById("title").textContent = DATA.title;
for (const b of document.querySelectorAll("[data-view]"))
  b.addEventListener("click", () => { state.view = b.dataset.view; LS.set("view", state.view); applyView(); render(); });
document.getElementById("theme").addEventListener("click", () => {
  state.theme = state.theme === "dark" ? "light" : "dark"; LS.set("theme", state.theme); applyTheme();
});
const q = document.getElementById("q");
q.addEventListener("input", () => { state.q = q.value.trim().toLowerCase(); render(); });
document.addEventListener("keydown", e => {
  if (document.querySelector("dialog[open]")) return;
  if (e.key === "/" && document.activeElement !== q && !/INPUT|TEXTAREA/.test(document.activeElement.tagName)) { e.preventDefault(); q.focus(); }
  if (e.key === "Escape" && document.activeElement === q) { q.value = ""; state.q = ""; render(); q.blur(); }
});
document.getElementById("addrole").addEventListener("submit", e => {
  e.preventDefault();
  const inp = document.getElementById("newrole");
  const name = inp.value.trim().replace(/^arn:[^:]+:iam::\d{12}:role\//, "");
  if (!name) return;
  if (state.roles.includes(name)) { toast(`${name} already added`); return; }
  state.roles.push(name); inp.value = ""; saveRoles();
});
document.getElementById("collapse").addEventListener("click", () => {
  const groups = [...document.querySelectorAll("details.group")];
  const open = !groups.some(g => g.open);
  groups.forEach(g => { g.open = open; });
});
document.getElementById("export").addEventListener("click", exportBookmarks);
document.getElementById("rolesBtn").addEventListener("click", openRoles);
document.getElementById("advBtn").addEventListener("click", () => document.getElementById("advDlg").showModal());
for (const dlg of document.querySelectorAll("dialog")) {
  dlg.querySelector(".dlg-close").addEventListener("click", () => dlg.close());
  dlg.addEventListener("click", e => { if (e.target === dlg) dlg.close(); });  // backdrop
}
applyTheme(); applyView(); renderOrgs(); renderEnvs(); renderRoles(); renderOrgRoles(); render();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
