# Gitea Repository Mirror Wiki

Gitea Repository Mirror backs up every repository your Gitea token can read into a local directory. It is designed to run repeatedly.

## Quick Start

```bash
python setup.py --venv
python gitea_repository_mirror.py
```

Manual install:

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python gitea_repository_mirror.py
```

On Linux or macOS, use `source .venv/bin/activate`.

## Token Setup

Create a Gitea token with read access. Recommended scopes depend on the Gitea version, but generally include repository read access and user read access.

Set the token in an environment variable:

```powershell
$env:GITEA_TOKEN="YOUR_TOKEN_HERE"
```

or on cmd.exe:

```bat
set GITEA_TOKEN=YOUR_TOKEN_HERE
```

The default variable name is controlled by `gitea.token_env` in `config.json`.

## Configuration

On first run, `config.json` is created from `config-default.json`.

Set at minimum:

```json
{
  "gitea": {
    "base_url": "https://git.example.com"
  },
  "sync": {
    "output_dir": "D:/GiteaMirror"
  }
}
```

## Dry Run First

The default config uses dry-run mode. Use the GUI's dry-run/live controls before allowing filesystem changes.

For CLI use:

```bash
python gitea_repository_mirror.py --dry-run
python gitea_repository_mirror.py --no-dry-run
```

## Output Layout

The mirror root contains cloned repositories, `_repo_sync_manifest.json`, and optionally `_archive/` for repositories that disappeared from the remote list.

## Keep Local State Private

`config.json`, manifests, logs, and mirror output may contain private paths or repository names. They are runtime state and should not be committed.
