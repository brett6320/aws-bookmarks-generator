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

The table is configured by ARN (BG_DDB_TABLE_ARN in .env, or --ddb-table-arn); its
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
# Legacy accounts that are Dev, or have "sandbox" in their name, are sandboxes in practice. The name
# rule never downgrades these (their console colour is a safety cue); they're logged instead.
LEGACY_SANDBOX_ENV = "Sbx"
LEGACY_SANDBOX_KEEP = {"Prd", "Bil"}
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
    "ddb_table_arn": ("BG_DDB_TABLE_ARN", None),
    "ddb_profile": ("BG_DDB_PROFILE", None),
    "ddb_role_name": ("BG_DDB_ROLE_NAME", "OrganizationAccountAccessRole"),
    "attr_account_id": ("BG_ATTR_ACCOUNT_ID", "accountNumber"),
    "attr_short_name": ("BG_ATTR_SHORT_NAME", "accountShortCode"),
    "attr_ou": ("BG_ATTR_OU", "ou"),
    "attr_environment": ("BG_ATTR_ENVIRONMENT", "environment"),
    "roles": ("BG_DEFAULT_ROLES", "ReadOnly,Administrator"),
    "title": ("BG_TITLE", "AWS Accounts"),
    "output_dir": ("BG_OUTPUT_DIR", "output"),
    "include_inactive": ("BG_INCLUDE_INACTIVE", "false"),
    "org_label": ("BG_ORG_LABEL", None),
    "org_roles": ("BG_ORG_ROLES", None),
    "omit_global_roles": ("BG_ORG_OMIT_GLOBAL_ROLES", "false"),
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

    A key present in `env` but blank means the default, not the shell's value. BG_ORG_* keys
    are per org, so with a named env file they come only from that file."""
    unknown = set(kwargs) - set(SETTINGS)
    if unknown:
        raise TypeError(f"Unknown setting(s): {', '.join(sorted(unknown))}")
    # settings were prefixed BM_ before; they're ignored now, so say so
    legacy = sorted(k for k in set(env or {}) | set(os.environ)
                    if k.startswith("BM_") and "BG_" + k[3:] in {v for v, _ in SETTINGS.values()})
    if legacy:
        log.warning("Ignoring %s: settings are now prefixed BG_ (rename to %s)",
                    ", ".join(legacy), ", ".join("BG_" + k[3:] for k in legacy))
    cfg: Dict[str, Any] = {}
    for key, (env_var, default) in SETTINGS.items():
        value = kwargs.get(key)
        if value is None:
            if env is not None and (env_var in env or env_var.startswith("BG_ORG_")):
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
        raise ValueError("ddb_table_arn is required (BG_DDB_TABLE_ARN in .env, --ddb-table-arn, or kwarg)")
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
                           "fix --ddb-profile / BG_DDB_PROFILE")
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


def _legacy_sandbox(domain: str, env: Optional[str], *names: str) -> bool:
    """Legacy (any case) and Dev, or named *sandbox* unless it's Prd/Bil."""
    if domain.lower() != LEGACY_DOMAIN.lower():
        return False
    return env == "Dev" or (env not in LEGACY_SANDBOX_KEEP and any("sandbox" in n.lower() for n in names))


def merge(org: Dict[str, Any], meta: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged = []
    remapped = 0
    for acct_id, info in org["accounts"].items():
        m = meta.get(acct_id, {})
        ou = (str(m["ou"]).strip() if m.get("ou") not in (None, "") else "")
        env_raw = m.get("envRaw")
        env = normalize_env(env_raw)
        if env_raw not in (None, "") and env is None:
            log.warning("Account %s: unrecognised environment %r", acct_id, env_raw)
        # short codes are lowercase by convention; the account-name fallback keeps its case
        short = str(m["shortName"]).strip().lower() if not _blank(m.get("shortName")) else info["name"]
        domain = ou or LEGACY_DOMAIN
        mapped = env != LEGACY_SANDBOX_ENV and _legacy_sandbox(domain, env, info["name"], short)
        if not mapped and env in LEGACY_SANDBOX_KEEP and domain.lower() == LEGACY_DOMAIN.lower() \
                and any("sandbox" in n.lower() for n in (info["name"], short)):
            log.warning("Account %s (%s): Legacy, named like a sandbox but %s in the table; left as %s",
                        acct_id, info["name"], env, env)
        if mapped:
            log.debug("Account %s (%s): Legacy %s -> %s", acct_id, info["name"], env or "no environment", LEGACY_SANDBOX_ENV)
            env, remapped = LEGACY_SANDBOX_ENV, remapped + 1
        merged.append(
            {
                **info,
                "shortName": short,
                "domain": domain,
                "env": env,
                "envRaw": None if env_raw is None else str(env_raw),
                "envMapped": mapped,
                "hasMetadata": bool(m),
            }
        )
    orphans = set(meta) - set(org["accounts"])
    if orphans:
        log.warning("%d DDB entries match no active org account: %s", len(orphans), ", ".join(sorted(orphans)[:10]))
    if remapped:
        log.info("%d %s accounts (Dev, or \"sandbox\" in the name) shown as %s", remapped, LEGACY_DOMAIN, LEGACY_SANDBOX_ENV)
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
            raise ValueError(f"Org label(s) {', '.join(bad)} contain ':'; set BG_ORG_LABEL without one")
        dupes = sorted({label for label in labels if labels.count(label) > 1})
        if dupes:
            raise ValueError(f"Duplicate org label(s) {', '.join(dupes)}; set BG_ORG_LABEL in each env file")

    orgs: List[Dict[str, Any]] = []
    accounts: List[Dict[str, Any]] = []
    failed: List[str] = []
    for cfg in cfgs:
        label = cfg["org_label"]
        if multi:
            log.info("Org %s", label)
        if cfg["omit_global_roles"] and not cfg["org_roles"]:
            log.warning("Org %s omits the global roles but has no org roles; it starts with no links until "
                        "roles are added in the page",
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
            log.debug("Org %s omits the global roles; its BG_DEFAULT_ROLES aren't added to them", cfg["org_label"])
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
    p.add_argument("--ddb-table-arn", help="ARN of the account metadata table (default BG_DDB_TABLE_ARN)")
    p.add_argument("--ddb-profile", help="Profile for the table's account (default: --profile, or assume --ddb-role-name)")
    p.add_argument("--ddb-role-name", help="Role assumed in the table's account when --ddb-profile isn't set")
    p.add_argument("--attr-account-id", help="DDB attribute: account number (default accountNumber)")
    p.add_argument("--attr-short-name", help="DDB attribute: short name (default accountShortCode)")
    p.add_argument("--attr-ou", help="DDB attribute: OU / business domain (default ou)")
    p.add_argument("--attr-environment", help="DDB attribute: environment (default environment)")
    p.add_argument("--roles", help="Comma-separated roles seeded into the page on first load")
    p.add_argument("--org-roles", help="Comma-separated roles added for this org only (default BG_ORG_ROLES)")
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
button,input,select{font:inherit;color:inherit}
[hidden]{display:none!important}
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
.domsel{max-width:200px;padding:5px 8px;border-radius:6px;border:1px solid var(--line);background:var(--bg);color:var(--text)}
.domsel.on{border-color:var(--accent)}
.chip{border:1px solid var(--line);background:transparent;border-radius:999px;padding:3px 9px;cursor:pointer;display:inline-flex;gap:5px;align-items:center;color:var(--muted)}
.chip .dot{width:8px;height:8px;border-radius:50%;background:var(--c)}
.chip[aria-pressed="true"]{border-color:var(--c);color:var(--text);background:color-mix(in srgb,var(--c) 18%,transparent)}
.chip b{font-weight:500;color:var(--muted);font-size:11px}
.btn{border:1px solid var(--line);background:var(--card);border-radius:6px;padding:5px 10px;cursor:pointer}
.btn:hover{background:var(--card-h)}
.btn b{font-weight:600;color:var(--muted);margin-left:2px}
dialog{border:1px solid var(--line);border-radius:10px;background:var(--panel);color:var(--text);padding:0;
  width:min(460px,calc(100vw - 32px));max-height:min(85vh,720px);box-shadow:0 12px 40px rgba(0,0,0,.45)}
dialog::backdrop{background:rgba(0,0,0,.55)}
.dlg{display:flex;flex-direction:column;gap:12px;padding:16px;max-height:min(85vh,720px);overflow:auto}
.sect{display:flex;flex-direction:column;gap:8px;border-top:1px solid var(--line);padding-top:12px}
.sect h3{margin:0;font-size:13px}
.sect-head{display:flex;align-items:center;gap:8px;justify-content:space-between}
.orgtoggles{display:flex;flex-direction:column;gap:4px}
.orgt{display:flex;align-items:center;gap:8px;padding:5px 8px;border:1px solid var(--line);border-radius:5px;background:var(--card);cursor:pointer}
.orgt span{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.orgt small{color:var(--muted);font:11px ui-monospace,SFMono-Regular,Menlo,monospace}
.orgt b{font-weight:500;color:var(--muted);font-size:11px}
.btn svg{width:14px;height:14px;vertical-align:-2px;margin-right:4px}
.dlg-head{display:flex;align-items:center;gap:8px}
.dlg-head h2{font-size:15px;margin:0;flex:1}
.dlg-head button{border:0;background:transparent;color:var(--muted);font-size:20px;line-height:1;cursor:pointer;padding:0 4px}
.dlg-head button:hover{color:var(--text)}
.roles{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:4px;overflow:auto;max-height:40vh}
.roles:empty::after{content:"No roles yet. Add one below.";color:var(--muted);font-size:12px}
.roles li{display:flex;align-items:center;gap:6px;padding:6px 8px;cursor:grab;border-radius:5px;background:var(--card);border:1px solid var(--line)}
.roles li span{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.roles li button{border:0;background:transparent;cursor:pointer;color:var(--muted);padding:0 2px;font-size:14px;line-height:1}
.roles li button:hover{color:var(--text)}
.roles li button.rm:hover{color:var(--prd)}
.roles li button:disabled{visibility:hidden}
.roles li input{width:130px;flex:none;padding:3px 6px;border-radius:4px;border:1px solid var(--line);background:var(--bg);font-size:12px}
.rolebar{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.rolebar .btn{padding:3px 9px;font-size:12px}
.rolebar .btn.warn{border-color:var(--prd);color:var(--prd)}
#rolesStatus:empty{display:none}
#rolesStatus.err{color:var(--prd)}
.roles li.drag{opacity:.4}
.addrole{display:flex;gap:6px}
.roleorg{padding:5px 8px;border-radius:5px;border:1px solid var(--line);background:var(--bg);color:var(--text)}
.addrole input{flex:1;min-width:0;padding:5px 8px;border-radius:5px;border:1px solid var(--line);background:var(--bg)}
.hint{color:var(--muted);font-size:11px;margin:0}
main{flex:1;overflow:auto;padding:12px 16px 24px;min-height:0}
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
  <select class="domsel" id="domain" aria-label="Business domain filter" title="Show one business domain"></select>
  <div class="envs" id="envs" role="group" aria-label="Environment filter"></div>
  <button class="btn" id="collapse" title="Collapse / expand all">⇕</button>
  <button class="btn" id="theme" title="Toggle light/dark"></button>
  <button class="btn" id="settingsBtn" title="Organizations, roles and export" aria-haspopup="dialog"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>Settings</button>
</header>
<main id="main"></main>
<dialog id="settingsDlg" aria-labelledby="settingsTitle">
  <div class="dlg">
    <div class="dlg-head"><h2 id="settingsTitle">Settings</h2><button class="dlg-close" aria-label="Close">×</button></div>
    <section class="sect" id="orgSect" hidden>
      <h3>Organizations</h3>
      <div class="orgtoggles" id="orgs"></div>
      <p class="hint">Orgs switched off are hidden from every view and from the export. Remembered in this browser.</p>
    </section>
    <section class="sect">
      <div class="sect-head"><h3>Roles</h3><select class="roleorg" id="roleOrg" aria-label="Organization" hidden></select></div>
      <ul class="roles" id="roles"></ul>
      <form class="addrole" id="addrole"><input id="newrole" placeholder="Role name or role ARN" aria-label="Role name"><button class="btn">Add</button></form>
      <p class="hint">Saved in this browser, per org. Each account shows a switch-role link per role, in this order (drag, or use ↑ ↓). A display name replaces the role name on links, in the console's role menu, and in exported bookmarks. Roles added to the config later appear automatically; roles you remove stay removed.</p>
      <p class="hint" id="orgRoles"></p>
      <div class="rolebar">
        <button class="btn" id="rolesExport" type="button" title="Download every org's roles, order and display names as JSON">Export roles</button>
        <button class="btn" id="rolesImport" type="button" title="Load roles from a JSON file exported here">Import roles…</button>
        <input type="file" id="rolesFile" accept=".json,application/json" hidden>
        <button class="btn" id="rolesReset" type="button">Reset to config</button>
      </div>
      <p class="hint" id="rolesStatus" role="status" aria-live="polite"></p>
    </section>
    <section class="sect">
      <h3>Export</h3>
      <div class="adv-item">
        <p>Download the accounts currently shown (search and filters applied) as a bookmarks file, in folders matching the current view. Import it from your browser's bookmarks menu.</p>
        <button class="btn" id="export">Export bookmarks</button>
      </div>
    </section>
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
// Each org keeps its own role list in this browser, seeded from its config roles: BG_DEFAULT_ROLES
// plus BG_ORG_ROLES, or only BG_ORG_ROLES when the org omits the global roles. Config roles added
// later are appended once; roles the user removed stay removed (tracked in awsbm.seenDefaults:<org>).
const configRoles = o => o.omitGlobalRoles ? o.roles
  : [...DATA.defaultRoles, ...o.roles.filter(r => !DATA.defaultRoles.includes(r))];
const ROLE_ARN_PREFIX = /^arn:[^:]+:iam::\d{12}:role\//;
// role name from a name or ARN (a path, e.g. aws-reserved/sso.amazonaws.com/, is kept); null if invalid
function cleanRole(raw) {
  const r = String(raw).trim().replace(ROLE_ARN_PREFIX, "");
  return /^[\w+=,.@-]+(\/[\w+=,.@-]+)*$/.test(r) && r.length <= 576 ? r : null;
}
const roleKey = i => ":" + (MULTI ? DATA.orgs[i].label : "");
function initRoles(o, i) {
  const defs = configRoles(o), k = roleKey(i);
  let roles = LS.get("roles" + k, null), seen;
  if (roles) seen = LS.get("seenDefaults" + k, defs);
  else {
    // one shared list ("roles") came before per-org lists; carry it over where it applied
    const legacy = o.omitGlobalRoles ? null : LS.get("roles", null);
    roles = legacy ? [...legacy] : [];
    seen = legacy ? LS.get("seenDefaults", DATA.defaultRoles) : [];
  }
  seen = new Set(seen);
  for (const r of defs) if (!seen.has(r) && !roles.includes(r)) roles.push(r);
  LS.set("roles" + k, roles);
  LS.set("seenDefaults" + k, [...new Set([...seen, ...defs])]);
  return roles;
}
// display names per org, role -> name; stored as [role, name] pairs (role names are user data)
const initNames = (o, i) => new Map([LS.get("roleNames" + roleKey(i), [])].flat()
  .filter(p => Array.isArray(p) && typeof p[0] === "string" && typeof p[1] === "string"));
const state = {
  view: LS.get("view", "domain"),
  theme: LS.get("theme", "dark"),
  roles: DATA.orgs.map(initRoles),  // per org, indexed like DATA.orgs
  names: DATA.orgs.map(initNames),  // per org, role -> display name
  roleOrg: 0,                       // org whose list the Roles dialog edits
  envs: new Set(LS.get("envs", [])),
  domain: String(LS.get("domain", "") || ""),  // business domain filter; "" = all
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
const rolesFor = a => state.roles[a.org];
const roleLabel = (org, role) => state.names[org].get(role) || role;
function displayName(a, role) {
  return `${a.shortName} / ${roleLabel(a.org, role)}`.slice(0, 64);
}
function switchUrl(a, role) {
  const p = new URLSearchParams({account: a.id, roleName: role, displayName: displayName(a, role)});
  if (ENV_CONSOLE[a.env]) p.set("color", ENV_CONSOLE[a.env]);
  return `${DATA.orgs[a.org].signinUrl}/switchrole?${String(p).replace(/\+/g, "%20")}`;
}

/* ---------- filtering ---------- */
const orgOn = a => !MULTI || !state.hiddenOrgs.has(DATA.orgs[a.org].label);
// skip: "env" or "domain" to leave that filter out (faceted counts on the filter controls)
function visible(a, skip) {
  if (!orgOn(a)) return false;
  if (skip !== "env" && state.envs.size && !state.envs.has(a.env || "—")) return false;
  if (skip !== "domain" && state.domain && a.domain !== state.domain) return false;
  if (!state.q) return true;
  const hay = [a.shortName, a.name, a.id, a.domain, a.env, a.envRaw, a.email, ...a.orgPath,
    MULTI ? DATA.orgs[a.org].label : ""].join(" ").toLowerCase();
  return state.q.split(/\s+/).every(t => hay.includes(t));
}

/* ---------- rendering ---------- */
function card(a) {
  const roles = rolesFor(a);
  const links = roles.length
    ? roles.map(r => el("a", {href: switchUrl(a, r), target: "_blank", rel: "noopener", title: `Switch to ${r} in ${a.id}`}, roleLabel(a.org, r)))
    : [el("button", {class: "linkish", onclick: () => openSettings(a.org)}, "Add a role to get links")];
  return el("div", {class: "card", style: `--c:${envColor(a.env)}`},
    el("div", {class: "ctop"},
      el("strong", {title: a.shortName}, a.shortName),
      el("span", {class: "badge", title: (a.envRaw || "No environment") + (a.envMapped ? ` (shown as ${a.env}: Legacy dev/sandbox account)` : "")}, a.env || "—")),
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
  main.replaceChildren(...(nodes.length ? nodes : [emptyState()]));
  main.scrollTop = scroll;
  const shown = DATA.accounts.filter(visible).length;
  document.getElementById("sub").textContent =
    `${shown} of ${DATA.accounts.length} accounts` + (MULTI ? ` in ${DATA.orgs.length} org${DATA.orgs.length === 1 ? "" : "s"}` : "") +
    ` · generated ${DATA.generatedAt}` + (DATA.failedOrgs.length ? ` · not read: ${DATA.failedOrgs.join(", ")}` : "");
}
// filters are remembered across renders (shared localStorage), so say which ones hide everything
function emptyState() {
  const on = [state.domain && `business domain "${state.domain}"`, state.envs.size && `environment ${[...state.envs].join(", ")}`,
    state.q && `search "${state.q}"`].filter(Boolean);
  return el("div", {class: "empty"}, "No accounts match" + (on.length ? ` (filtered by ${on.join(", ")}).` : "."),
    on.length ? [" ", el("button", {class: "linkish", onclick: clearFilters}, "Clear filters")] : null);
}
function clearFilters() {
  state.domain = ""; state.envs.clear(); state.q = ""; q.value = "";
  LS.set("domain", ""); LS.set("envs", []); refresh();
}
const refresh = () => { renderEnvs(); renderDomains(); render(); };
function renderEnvs() {
  const counts = {}, present = new Set();
  for (const a of DATA.accounts) if (orgOn(a)) {
    present.add(a.env || "—");
    if (visible(a, "env")) counts[a.env || "—"] = (counts[a.env || "—"] || 0) + 1;
  }
  const box = document.getElementById("envs");
  box.replaceChildren(...[...ENV_ORDER, "—"].filter(e => present.has(e) || ENV_ORDER.includes(e) || state.envs.has(e)).map(e =>
    el("button", {class: "chip", style: `--c:${envColor(e)}`, "aria-pressed": String(state.envs.has(e)),
      title: e === "—" ? "No environment set" : `Show only ${e} (multi-select)`,
      onclick: () => { state.envs.has(e) ? state.envs.delete(e) : state.envs.add(e); LS.set("envs", [...state.envs]); refresh(); }},
      el("span", {class: "dot"}), e, el("b", {}, counts[e] || 0))));
}
// one business domain at a time; options are the domains of orgs switched on, merged by name
function renderDomains() {
  const counts = new Map();  // domain names are table data: no plain-object keys
  for (const a of DATA.accounts) if (orgOn(a)) counts.set(a.domain, (counts.get(a.domain) || 0) + (visible(a, "domain") ? 1 : 0));
  // keep a remembered choice listed (count 0) so the filter is never applied invisibly
  if (state.domain && !counts.has(state.domain)) counts.set(state.domain, 0);
  const names = [...counts.keys()]
    .sort((x, y) => (x === DATA.legacyDomain) - (y === DATA.legacyDomain) || x.localeCompare(y));
  const sel = document.getElementById("domain");
  sel.replaceChildren(el("option", {value: ""}, "All business domains"),
    ...names.map(d => el("option", {value: d, selected: d === state.domain}, `${d} (${counts.get(d)})`)));
  sel.value = state.domain;
  sel.hidden = names.length < 2 && !state.domain;  // e.g. no OU values: everything is Legacy
  sel.classList.toggle("on", !!state.domain);
}
// combined page: switch orgs on/off in Settings; remembered per browser (e.g. one browser profile per org)
function renderOrgs() {
  document.getElementById("orgSect").hidden = !MULTI;
  if (!MULTI) return;
  document.getElementById("orgs").replaceChildren(...DATA.orgs.map((o, i) => el("label", {class: "orgt"},
    el("input", {type: "checkbox", checked: !state.hiddenOrgs.has(o.label),
      onchange: e => {
        e.target.checked ? state.hiddenOrgs.delete(o.label) : state.hiddenOrgs.add(o.label);
        LS.set("hiddenOrgs", [...state.hiddenOrgs]); refresh();
      }}),
    el("span", {}, o.label), o.orgId ? el("small", {}, o.orgId) : null,
    el("b", {title: "Accounts"}, DATA.accounts.filter(a => a.org === i).length))));
}
function renderRoles() {
  const ul = document.getElementById("roles");
  const list = state.roles[state.roleOrg], names = state.names[state.roleOrg];
  let dragFrom = null;
  const move = (from, to, dir) => {
    const [m] = list.splice(from, 1); list.splice(to, 0, m); saveRoles();
    if (!dir) return;  // keyboard/click move: keep focus on the moved row's arrow
    const li = document.getElementById("roles").children[to];
    const b = li && (li.querySelector(`[data-dir="${dir}"]:not(:disabled)`) || li.querySelector("[data-dir]:not(:disabled)"));
    if (b) b.focus();
  };
  ul.replaceChildren(...list.map((r, i) => el("li", {draggable: "true",
      ondragstart: e => { dragFrom = i; e.dataTransfer.setData("text/plain", r); e.dataTransfer.effectAllowed = "move"; e.currentTarget.classList.add("drag"); },
      ondragend: e => e.currentTarget.classList.remove("drag"),
      ondragover: e => e.preventDefault(),
      ondrop: e => { e.preventDefault(); if (dragFrom === null || dragFrom === i) return; move(dragFrom, i); }},
    el("span", {title: r}, r),
    // text selection and caret moves inside the input must not start a row drag
    el("input", {value: names.get(r) || "", placeholder: "Display name", maxlength: "64", "aria-label": `Display name for ${r}`,
      ondragstart: e => { e.preventDefault(); e.stopPropagation(); },
      onmousedown: e => e.currentTarget.closest("li").setAttribute("draggable", "false"),
      onblur: e => e.currentTarget.closest("li").setAttribute("draggable", "true"),
      onchange: e => { const v = e.target.value.trim(); v && v !== r ? names.set(r, v) : names.delete(r); saveNames(); render(); }}),
    el("button", {title: "Move up", "aria-label": `Move ${r} up`, "data-dir": "up", disabled: i === 0, onclick: () => move(i, i - 1, "up")}, "↑"),
    el("button", {title: "Move down", "aria-label": `Move ${r} down`, "data-dir": "down", disabled: i === list.length - 1, onclick: () => move(i, i + 1, "down")}, "↓"),
    el("button", {class: "rm", title: `Remove ${r}`, "aria-label": `Remove ${r}`,
      onclick: () => { list.splice(i, 1); names.delete(r); saveNames(); saveRoles(); }}, "×"))));
  const defs = configRoles(DATA.orgs[state.roleOrg]);
  document.getElementById("orgRoles").textContent = `From config: ${defs.join(", ") || "none"}`;
  const sel = document.getElementById("roleOrg");
  sel.hidden = !MULTI;
  if (MULTI) sel.replaceChildren(...DATA.orgs.map((o, i) =>
    el("option", {value: String(i), selected: i === state.roleOrg}, o.label)));
}
function openSettings(org) {
  // default to the first org switched on unless the current one still is
  if (typeof org === "number") state.roleOrg = org;
  else if (state.hiddenOrgs.has(DATA.orgs[state.roleOrg].label)) {
    const i = DATA.orgs.findIndex(o => !state.hiddenOrgs.has(o.label));
    if (i >= 0) state.roleOrg = i;
  }
  disarmReset(); rolesStatus(""); renderOrgs(); renderRoles();
  const dlg = document.getElementById("settingsDlg");
  if (!dlg.open) dlg.showModal();
  if (typeof org === "number") document.getElementById("newrole").focus();
}
function saveRoles() { LS.set("roles" + roleKey(state.roleOrg), state.roles[state.roleOrg]); renderRoles(); render(); }
// result of an import, reset or add, shown inside the dialog (a toast would sit under the modal)
function rolesStatus(msg, err) {
  const p = document.getElementById("rolesStatus");
  p.textContent = msg; p.classList.toggle("err", !!err);
}
function saveNames(i = state.roleOrg) { LS.set("roleNames" + roleKey(i), [...state.names[i]]); }

/* ---------- role lists: reset, JSON export / import ---------- */
// back to the org's config roles, in config order, without display names (two clicks)
function disarmReset() {
  clearTimeout(resetRoles.h);
  const btn = document.getElementById("rolesReset");
  btn.classList.remove("warn"); btn.textContent = "Reset to config";
}
function resetRoles(btn) {
  if (!btn.classList.contains("warn")) {
    btn.classList.add("warn"); btn.textContent = "Click again to reset";
    clearTimeout(resetRoles.h); resetRoles.h = setTimeout(disarmReset, 4000);
    return;
  }
  disarmReset();
  const i = state.roleOrg, defs = configRoles(DATA.orgs[i]);
  state.roles[i] = [...defs]; state.names[i] = new Map();
  LS.set("seenDefaults" + roleKey(i), defs); saveNames(i); saveRoles();
  rolesStatus(MULTI ? `Roles for ${DATA.orgs[i].label} reset to config` : "Roles reset to config");
}
const ROLES_FORMAT = "aws-bookmarks-roles";
function exportRoles() {
  const doc = {format: ROLES_FORMAT, version: 1, exportedAt: new Date().toISOString(),
    orgs: DATA.orgs.map((o, i) => ({label: o.label, orgId: o.orgId || null,
      roles: state.roles[i].map(r => state.names[i].has(r) ? {name: r, displayName: state.names[i].get(r)} : {name: r})}))};
  const url = URL.createObjectURL(new Blob([JSON.stringify(doc, null, 2) + "\n"], {type: "application/json"}));
  el("a", {href: url, download: "aws-bookmarks-roles.json"}).click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
// accepts this page's export, or a bare list of role names / {name, displayName} for the selected org
function parseRoleList(items) {
  if (!Array.isArray(items)) throw new Error("roles must be a list");
  const roles = [], names = new Map();
  for (const it of items) {
    const raw = typeof it === "string" ? it : it && typeof it.name === "string" ? it.name : null;
    if (raw === null) throw new Error("each role needs a name");
    const r = cleanRole(raw);
    if (!r) throw new Error(`not a valid role name: ${raw.slice(0, 80)}`);
    if (roles.includes(r)) continue;
    roles.push(r);
    const dn = it && typeof it.displayName === "string" ? it.displayName.trim().slice(0, 64) : "";
    if (dn && dn !== r) names.set(r, dn);
  }
  return {roles, names};
}
function importRoles(text) {
  let doc;
  try { doc = JSON.parse(text); } catch (e) { throw new Error("not a JSON file"); }
  let entries;  // [[org index, {roles, names}]]
  if (Array.isArray(doc)) entries = [[state.roleOrg, parseRoleList(doc)]];
  else if (doc && doc.format === ROLES_FORMAT && Array.isArray(doc.orgs)) {
    if (doc.version !== 1) throw new Error(`unsupported roles file version ${doc.version}`);
    const file = doc.orgs.filter(e => e && typeof e === "object");
    // every org ID match first, then labels for what's left (an org ID can be missing on either side);
    // a file with one org applies to the selected org if nothing matches
    const hits = new Map(), used = new Set();
    DATA.orgs.forEach((o, i) => {
      const e = o.orgId && file.find(e => !used.has(e) && e.orgId === o.orgId);
      if (e) { hits.set(i, e); used.add(e); }
    });
    DATA.orgs.forEach((o, i) => {
      if (hits.has(i)) return;
      const e = file.find(e => !used.has(e) && e.label === o.label && !(o.orgId && e.orgId));
      if (e) { hits.set(i, e); used.add(e); }
    });
    if (!hits.size && file.length === 1) hits.set(state.roleOrg, file[0]);
    if (!hits.size) throw new Error("no org in the file matches this page");
    entries = [...hits].sort((x, y) => x[0] - y[0]).map(([i, e]) => [i, parseRoleList(e.roles)]);
  } else throw new Error("not a roles file exported from this page");
  for (const [i, {roles, names}] of entries) {
    state.roles[i] = roles; state.names[i] = names;
    LS.set("roles" + roleKey(i), roles);
    // the imported list is authoritative: config roles it leaves out stay out
    LS.set("seenDefaults" + roleKey(i), [...new Set([...LS.get("seenDefaults" + roleKey(i), []), ...configRoles(DATA.orgs[i])])]);
    saveNames(i);
  }
  renderRoles(); render();
  return entries.map(([i]) => DATA.orgs[i].label);
}
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
  if (!DATA.accounts.some(linked)) { toast("No shown account has roles; add one above"); return; }
  const esc = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const ts = Math.floor(Date.now() / 1000);
  const links = (accts, pad) => accts.flatMap(a => rolesFor(a).map(r =>
    `${pad}<DT><A HREF="${esc(switchUrl(a, r))}" ADD_DATE="${ts}">${esc(`${a.shortName} (${a.env || "—"}) · ${roleLabel(a.org, r)}`)}</A>`));
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
q.addEventListener("input", () => { state.q = q.value.trim().toLowerCase(); refresh(); });
document.getElementById("domain").addEventListener("change", e => {
  state.domain = e.target.value; LS.set("domain", state.domain); refresh();
});
document.addEventListener("keydown", e => {
  if (document.querySelector("dialog[open]")) return;
  if (e.key === "/" && document.activeElement !== q && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) { e.preventDefault(); q.focus(); }
  if (e.key === "Escape" && document.activeElement === q) { q.value = ""; state.q = ""; refresh(); q.blur(); }
});
document.getElementById("addrole").addEventListener("submit", e => {
  e.preventDefault();
  const inp = document.getElementById("newrole");
  if (!inp.value.trim()) return;
  const name = cleanRole(inp.value);
  if (!name) { rolesStatus("Not a valid role name or role ARN", true); return; }
  const list = state.roles[state.roleOrg];
  if (list.includes(name)) { rolesStatus(`${name} is already in the list`, true); return; }
  list.push(name); inp.value = ""; rolesStatus(""); saveRoles();
});
document.getElementById("collapse").addEventListener("click", () => {
  const groups = [...document.querySelectorAll("details.group")];
  const open = !groups.some(g => g.open);
  groups.forEach(g => { g.open = open; });
});
document.getElementById("export").addEventListener("click", exportBookmarks);
document.getElementById("settingsBtn").addEventListener("click", () => openSettings());
document.getElementById("roleOrg").addEventListener("change", e => {
  state.roleOrg = Number(e.target.value); disarmReset(); rolesStatus(""); renderRoles();
});
document.getElementById("rolesExport").addEventListener("click", exportRoles);
document.getElementById("rolesReset").addEventListener("click", e => resetRoles(e.currentTarget));
const rolesFile = document.getElementById("rolesFile");
document.getElementById("rolesImport").addEventListener("click", () => rolesFile.click());
rolesFile.addEventListener("change", async () => {
  const f = rolesFile.files[0]; rolesFile.value = "";
  if (!f) return;
  try {
    if (f.size > 1e6) throw new Error("file is too large");
    const orgs = importRoles(await f.text());
    rolesStatus(MULTI ? `Imported roles for ${orgs.join(", ")}` : "Roles imported");
  } catch (err) { rolesStatus(`Import failed: ${err.message}. Nothing was changed.`, true); }
});
for (const dlg of document.querySelectorAll("dialog")) {
  dlg.querySelector(".dlg-close").addEventListener("click", () => dlg.close());
  dlg.addEventListener("click", e => { if (e.target === dlg) dlg.close(); });  // backdrop
}
applyTheme(); applyView(); renderOrgs(); renderEnvs(); renderDomains(); renderRoles(); render();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
