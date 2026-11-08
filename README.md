# AWS Bookmarks Generator

Builds one self-contained HTML page of AWS console switch-role links for every account in an AWS Organization. Account structure comes from **Organizations**. Short name, business domain, and environment come from a **DynamoDB** table, matched on account number. The page loads nothing from outside the file (no CDN, fonts, or assets), so you can open it straight from disk or host it anywhere.

The script is a single file, `aws_bookmarks.py`. Its only third-party dependency is `boto3`. `truststore` is used if it's installed (OS certificate store for TLS), but it isn't required.

It's read-only against AWS. The only write is the local HTML file, so there's no `--execute` flag.

Each run writes a new render to `output/aws-bookmarks-<UTC timestamp>.html` (for example `output/aws-bookmarks-20261107T183012Z.html`). Existing renders are never overwritten, so you can open several versions side by side. Roles, theme and view settings are stored in localStorage, which is shared across renders opened from the same origin (all `file://` pages in Chrome, for example).

## Page features

- **One org or several.** Pass one env file per org and the page shows each org as a top-level group (see [Multiple orgs](#multiple-orgs)).
- **Group by business domain or by org hierarchy.** Domain comes from the table's OU attribute. Accounts with no OU value, or with no table row, go under **Legacy**.
- **Environment badges and filters** for Lab, Sbx, Dev, Tst, Prd, and Bil (billing). Common spellings are normalized (`sandbox` → Sbx, `test` → Tst, `production` → Prd, `billing` → Bil). The console header color is set per environment (Bil uses the default header).
- **Settings dialog** (header button) holds org on/off (combined page), roles, and bookmark export.
- **Roles per org, stored in localStorage.** In Settings, add, remove, or drag to reorder; on a combined page, pick the org first. Every account shows one switch-role link per role of its org.
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
  - Role ARNs are reduced to the role name, and duplicates are dropped.
  - `BG_ORG_*` keys are read only from the org's own file, never from the shell.
- **Org on/off.** Settings has a switch per org. Switching one off hides that org's accounts in every view, in the environment counts, and in the export.
  - The choice is remembered in the browser (localStorage), so each browser profile can keep its own orgs switched on.
  - Like roles, it's shared across renders opened from the same origin.
- **Flags.** With several files, only the page flags (`--roles`, `--title`, `-o`) are allowed. Put everything else in the env files.
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
| `attr_short_name` | `BG_ATTR_SHORT_NAME` | `accountShortCode` | Short name (falls back to the org account name) |
| `attr_ou` | `BG_ATTR_OU` | `ou` | Business domain (empty/missing → Legacy) |
| `attr_environment` | `BG_ATTR_ENVIRONMENT` | `environment` | Environment |
| `roles` | `BG_DEFAULT_ROLES` | `ReadOnly,Administrator` | Default roles, comma-separated in display order. Seed each browser's list; roles added later are appended, roles a user removed stay removed |
| `title` | `BG_TITLE` | `AWS Accounts` | Page title |
| `output_dir` | `BG_OUTPUT_DIR` | `output` | Directory for timestamped renders (created if missing) |
| `include_inactive` | `BG_INCLUDE_INACTIVE` | `false` | Include suspended/closed accounts |
| `org_label` | `BG_ORG_LABEL` | env file name | Org name on a multi-org page |
| `org_roles` | `BG_ORG_ROLES` | — | Comma-separated roles added for this org's accounts only, after the global roles |
| `omit_global_roles` | `BG_ORG_OMIT_GLOBAL_ROLES` | `false` | Show only `BG_ORG_ROLES` for this org, leaving out the global roles |

Without `ddb_profile`, the table is read with the Organizations session if it's in the table's account, otherwise through `ddb_role_name` assumed from it. The generator refuses to run if the credentials end up in a different account from the ARN.

Flags: `-v` for debug logging, `-q` for warnings only. The generator logs a warning for table rows that match no org account, for org accounts with no table row, and for environment values it doesn't recognize.

## IAM permissions

Organizations profile:

```json
{"Effect": "Allow", "Action": ["organizations:ListRoots", "organizations:ListOrganizationalUnitsForParent",
  "organizations:ListAccountsForParent"], "Resource": "*"}
```

Plus `sts:GetCallerIdentity`, and `sts:AssumeRole` on the DDB role when it's assumed. The DDB principal needs `dynamodb:Scan` on the table.

## Platform notes

- **macOS / Windows / Linux:** pure Python plus boto3. If `truststore` is installed, the OS certificate store is used (Security.framework, Windows Certificate Store, system CA bundle). Otherwise botocore's bundled CAs are used.
- **Windows:** credentials file is `%USERPROFILE%\.aws\credentials`.
- **GovCloud / China:** the sign-in URL follows the partition of `region`.
