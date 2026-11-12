# AWS Bookmarks Generator

Builds one self-contained HTML page of AWS console switch-role links for every account in an AWS Organization. Account structure comes from **Organizations**. Short name, business domain, and environment come from a **DynamoDB** table, matched on account number. Short names missing from the table come from a `Shortname` account tag, then the account name. The page loads nothing from outside the file (no CDN, fonts, or assets), so you can open it straight from disk or host it anywhere.

The script is a single file, `aws_bookmarks.py`. Its only third-party dependency is `boto3`. `truststore` is used if it's installed (OS certificate store for TLS), but it isn't required.

It's read-only against AWS. The only write is the local HTML file, so there's no `--execute` flag.

Each run writes a new render to `output/aws-bookmarks-<UTC timestamp>.html` (for example `output/aws-bookmarks-20261107T183012Z.html`). Existing renders are never overwritten, so you can open several versions side by side. Roles, theme and view settings are stored in localStorage, which is shared across renders opened from the same origin (all `file://` pages in Chrome, for example).

## Page features

- **One org or several.** Pass one env file per org and the page shows each org as a top-level group (see [Multiple orgs](#multiple-orgs)).
- **Group by business domain or by org hierarchy.** Domain comes from the table's OU attribute. Accounts with no OU value, or with no table row, go under **Legacy**.
- **Business domain filter.** A header dropdown shows one business domain (the table's OU attribute) at a time, with Legacy last and an account count per domain. It applies to both views, the shown-accounts count, and the export, and the choice is remembered in the browser. Counts on the dropdown and the environment chips take the other filters and search into account. If every account is Legacy (no OU values), the dropdown is hidden.
  - Filters are shared across renders like roles, so when filters hide everything the page names them and offers **Clear filters**. On a combined page, the list merges domains by name across the orgs that are switched on.
- **Environment badges and filters** for Lab, Sbx, Dev, Tst, Prd, and Bil (billing). Common spellings are normalized (`sandbox` → Sbx, `test` → Tst, `production` → Prd, `billing` → Bil). The console header color is set per environment (Bil uses the default header).
- **Per-account overrides** in the env file win over the table and the Legacy sandbox rule: `BG_ACCOUNT_ENVIRONMENTS=111122223333=Prd,444455556666=Sbx` and `BG_ACCOUNT_DOMAINS=111122223333=Payments`.
  - Environments accept the same spellings as the table (`sandbox`, `production`, …). Account IDs may drop leading zeros.
  - A bad entry stops the run. IDs that match no account on the page are ignored, with one warning. Values can't contain `,`; only the first `=` splits an entry, and anything after it (including `#`) is part of the value.
  - A business domain of `legacy` in any case means the Legacy group.
  - Per org, like the other org settings, but unlike `BG_ORG_*` a file that leaves them out falls back to the shell. Account IDs are unique across orgs, so one exported list covering several orgs works.
  - Tooltips show the table's value: the environment badge, and the account name for the business domain. The table itself isn't changed.
- **Legacy sandboxes.** Legacy accounts whose environment is Dev, or whose account name or short name contains "sandbox" (any case), are shown as Sbx. The name rule never changes a Prd or Bil account (their console colour is a safety cue); those are logged as a warning instead. The badge tooltip shows the table's original value. The table itself isn't changed.
- **Settings dialog** (header button) holds org on/off (combined page), roles, and bookmark export.
- **Roles per org, stored in localStorage.** In Settings, add, remove, and reorder (drag, or the ↑ ↓ buttons); on a combined page, pick the org first. Every account shows one switch-role link per role of its org, in that order.
  - **Display names.** Give a role a short name (for example `Admin` for `AWSReservedSSO_AdministratorAccess_1a2b3c4d`). It's used for the link text, the console's role-history name, and exported bookmarks; the link still switches to the real role.
    - Set defaults in config with `BG_ROLE_NAMES=AWSReservedSSO_AdministratorAccess_1a2b3c4d=Admin,ReadOnlyAccess=Read only`. They apply to any role with that name, in every org.
    - The role must be written as it appears in the role list, including any path (`aws-reserved/sso.amazonaws.com/Name`). Display names can't contain `,`; everything before the last `=` is the role.
    - A name typed in Settings overrides the config's for that browser. Leave the box empty to use the config's name, which is shown greyed in the box, or type the role name itself to show it plain.
    - Exported role files hold only the names typed in Settings.
  - **Export / import roles.** *Export roles* downloads `aws-bookmarks-roles.json` with every org's roles, order and display names. *Import roles…* loads such a file, matching orgs by organization ID, then by label. Org IDs are matched first, so a label shared by two orgs can't pull in the wrong list. A file with one org that matches none, or a plain JSON list of role names (or `{"name", "displayName"}` objects), goes to the org selected in Settings. The imported list replaces the current one, and config roles it leaves out stay out. Import is all-or-nothing: one invalid role name rejects the file and nothing changes. Role names with a path (from an ARN such as `…:role/aws-reserved/sso.amazonaws.com/Name`) are kept as `path/Name`, both here and in the Add form.
  - **Reset to config** (click twice) puts the selected org back to its config roles, in config order, and clears the display names typed in Settings, so the config's names apply again.
  - Each org's list starts from its config roles: `BG_DEFAULT_ROLES` plus `BG_ORG_ROLES`, or only `BG_ORG_ROLES` when `BG_ORG_OMIT_GLOBAL_ROLES=true`.
  - Config roles added later are appended once, and roles you removed stay removed.
  - A browser list saved before per-org roles carries over to orgs that don't omit the global roles.
- **Light/dark mode** (dark by default), saved per browser.
- **Fits the viewport.** Only the account list scrolls. Groups can be collapsed, and the collapsed state is remembered.
- **Search** (press `/`) across short name, account name, ID, domain, environment, and OU path. Click an account ID to copy it.
- **Export bookmarks** (in Settings) downloads a Netscape-format bookmarks file for the visible accounts, with folders matching the current view. Every major browser can import it.

## Usage

```bash
pip install boto3

# Table from BG_DDB_TABLE_ARN in .env; read via a role assumed from the org profile
python aws_bookmarks.py --profile org-mgmt --ddb-role-name BookmarksReader

# Table read with its own profile, ARN given on the command line
python aws_bookmarks.py --profile org-mgmt --ddb-profile shared-services \
    --ddb-table-arn arn:aws:dynamodb:us-east-1:111122223333:table/account-mapping

# Several orgs, one combined page (one env file per org)
python aws_bookmarks.py --env-file prod.env --env-file lab.env

open "$(command ls -t output/*.html | head -1)"   # newest render
```

As a library, every setting can be passed as a kwarg:

```python
from aws_bookmarks import generate
generate(profile="org-mgmt", ddb_profile="shared-services",
         roles=["ReadOnly", "PowerUser"], output_dir="renders")  # returns the Path written
generate(env_files=["prod.env", "lab.env"])                    # combined multi-org page
```

## Multiple orgs

Each org gets its own env file, holding that org's `AWS_PROFILE`, `BG_DDB_TABLE_ARN` (one table per org), and any other per-org settings from the table below.

- **One env file** (`--env-file prod.env`), or none (`./.env`), gives a single-org page, `output/aws-bookmarks-<timestamp>.html`. It's the same page as before.
- **Two or more** give one combined page, `output/aws-bookmarks-all-<timestamp>.html`.
  - Each org is a collapsible top-level group showing its label and organization ID. Business Domains and Org Hierarchy nest inside it, and search matches the org label too.
  - In Org Hierarchy, the org's group is its root.
  - Bookmark export puts each org in its own folder.

Details:
- **Org label.** Set it with `BG_ORG_LABEL` in the file. Otherwise it comes from the file name: `prod.env`, `.env.prod` and `prod/.env` all give `prod`. Labels must be unique and can't contain `:`.
- **Page settings** (`BG_TITLE`, `BG_OUTPUT_DIR`) come from the first file.
- **Roles.** `BG_DEFAULT_ROLES` from every file is merged in order, without duplicates, into the global list.
  - Each org's starting list is the global roles plus its `BG_ORG_ROLES`. An org that sets `BG_ORG_OMIT_GLOBAL_ROLES=true` starts from `BG_ORG_ROLES` only, and its own `BG_DEFAULT_ROLES` is left out of the global roles.
  - After that, each org's list is edited separately in Settings.
  - `BG_ORG_ROLES` adds roles for that org only.
  - `BG_ROLE_NAMES` from every file is merged. If two files name the same role differently, the first file wins, with a warning. A file that sets `BG_ROLE_NAMES` wins over one that only inherits it from the shell.
  - Role ARNs are reduced to the role name, and duplicates are dropped.
  - `BG_ORG_*` keys are read only from the org's own file, never from the shell.
- **Org on/off.** Settings has a switch per org. Switching one off hides that org's accounts in every view, in the environment and business domain counts, and in the export.
  - The choice is remembered in the browser (localStorage), so each browser profile can keep its own orgs switched on.
  - Like roles, it's shared across renders opened from the same origin.
- **Flags.** With several files, only the page flags (`--roles`, `--role-names`, `--title`, `-o`) are allowed. Put everything else in the env files.
- **Precedence.** Values in a named env file override the shell environment, so an exported `AWS_PROFILE` doesn't leak into every org.
  - A key that's present but blank (`AWS_PROFILE=`) means the default, not the shell's value.
  - Keys a file leaves out entirely still fall back to the environment.
- **Other variables** (AWS credentials, `AWS_CA_BUNDLE`, proxies and so on) are process-wide, so they can't differ per org.
  - With one env file they're exported as before.
  - With several, they're ignored with a warning. Set them in the shell, and use profiles for per-org credentials.
- **If an org can't be read** (bad profile, access denied, unexpected data), it's left out. The page subtitle names it, and the CLI exits 1.
- **Same org twice.** Two files that resolve to the same organization (same org ID, or shared accounts when the ID can't be read) are refused.

`seed_ddb.py --env-file prod.env` seeds one org's table.

## Configuration

Precedence: kwargs / CLI flags > a file named with `--env-file` > environment (including `.env` in cwd, parsed natively) > defaults. Copy `.env.example` to `.env`, or to one file per org. Settings used to be prefixed `BM_`. Old `BM_*` names are now ignored, with a warning naming the `BG_*` replacement.

| kwarg / flag | Env var | Default | Description |
|---|---|---|---|
| `profile` | `AWS_PROFILE` | default chain | Profile for Organizations (management or delegated-admin account) |
| `region` | `AWS_REGION` | `us-east-1` | Region; also determines partition / sign-in URL |
| `ddb_table_arn` | `BG_DDB_TABLE_ARN` | **required** | Metadata table ARN; table name, account and region come from it |
| `ddb_profile` | `BG_DDB_PROFILE` | — | Profile for the table's account (takes precedence) |
| `ddb_role_name` | `BG_DDB_ROLE_NAME` | `OrganizationAccountAccessRole` | Role assumed in the table's account when no `ddb_profile` is set and `profile` is in a different account |
| `attr_account_id` | `BG_ATTR_ACCOUNT_ID` | `accountNumber` | Account number attribute (string or number; zero-padded to 12) |
| `attr_short_name` | `BG_ATTR_SHORT_NAME` | `accountShortCode` | Short name (falls back to the `tag_short_name` account tag, then the org account name) |
| `attr_ou` | `BG_ATTR_OU` | `ou` | Business domain (empty/missing → Legacy) |
| `attr_environment` | `BG_ATTR_ENVIRONMENT` | `environment` | Environment |
| `tag_short_name` | `BG_TAG_SHORT_NAME` | `Shortname` | Account tag (in Organizations) used for the short name when the table has none or can't be read. Exact key first, then any case; the value is lowercased like the table's. Only accounts without a table short name are looked up |
| `roles` | `BG_DEFAULT_ROLES` | `ReadOnly,Administrator` | Default roles, comma-separated in display order. Seed each browser's list; roles added later are appended, roles a user removed stay removed |
| `role_names` | `BG_ROLE_NAMES` | — | Display names for roles, `Role=Name` pairs, comma-separated (role ARNs accepted). Used on links, in the console's role history and in exported bookmarks; a name typed in Settings overrides it |
| `title` | `BG_TITLE` | `AWS Accounts` | Page title |
| `output_dir` | `BG_OUTPUT_DIR` | `output` | Directory for timestamped renders (created if missing) |
| `include_inactive` | `BG_INCLUDE_INACTIVE` | `false` | Include suspended/closed accounts |
| `org_label` | `BG_ORG_LABEL` | env file name | Org name on a multi-org page |
| `org_roles` | `BG_ORG_ROLES` | — | Comma-separated roles added for this org's accounts only, after the global roles |
| `account_environments` | `BG_ACCOUNT_ENVIRONMENTS` | — | Environment per account, overriding the table: `ID=Env`, comma-separated |
| `account_domains` | `BG_ACCOUNT_DOMAINS` | — | Business domain per account, overriding the table: `ID=Domain`, comma-separated |
| `omit_global_roles` | `BG_ORG_OMIT_GLOBAL_ROLES` | `false` | Show only `BG_ORG_ROLES` for this org, leaving out the global roles |

Without `ddb_profile`, the table is read with the Organizations session if it's in the table's account, otherwise through `ddb_role_name` assumed from it. The generator refuses to run if the credentials end up in a different account from the ARN.

If the table can't be read (access denied, endpoint unreachable), the org is still rendered, with a warning and exit code 0: short names come from the account tag or account name, and every account is Legacy with no environment, except for env-file overrides and Legacy accounts named like a sandbox (shown as Sbx). An unknown profile, missing credentials or a table that doesn't exist still stop the run.

Flags: `-v` for debug logging, `-q` for warnings only. The generator logs a warning for table rows that match no org account, for org accounts with no table row, and for environment values it doesn't recognize.

## IAM permissions

Organizations profile:

```json
{"Effect": "Allow", "Action": ["organizations:ListRoots", "organizations:ListOrganizationalUnitsForParent",
  "organizations:ListAccountsForParent"], "Resource": "*"}
```

Add `organizations:ListTagsForResource` for the short-name tag fallback. Without it the generator warns once and uses account names.

Plus `sts:GetCallerIdentity`, and `sts:AssumeRole` on the DDB role when it's assumed. The DDB principal needs `dynamodb:Scan` on the table.

## Platform notes

- **macOS / Windows / Linux:** pure Python plus boto3. If `truststore` is installed, the OS certificate store is used (Security.framework, Windows Certificate Store, system CA bundle). Otherwise botocore's bundled CAs are used.
- **Windows:** credentials file is `%USERPROFILE%\.aws\credentials`.
- **GovCloud / China:** the sign-in URL follows the partition of `region`.
