# Gitea Repository Mirror

GUI and CLI utility for mirroring repositories from a Gitea account or organization into a local folder.

## What It Does

- Clones repositories that are missing locally
- Updates repositories that already exist
- Optionally mirrors wiki repositories
- Archives local repositories that are no longer visible remotely
- Maintains a local manifest for repeatable syncs

## Requirements

- Python 3.10+
- Git installed and available on `PATH`
- Network access to a Gitea instance
- A Gitea token with read access
- Dependencies from `requirements.txt`

## Install

```bash
python setup.py --venv
```

Or manually:

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

On Linux or macOS, activate the virtual environment with `source .venv/bin/activate`.

## Configure

On first run, the app creates `config.json` from `config-default.json`.

Set at minimum:

- `gitea.base_url`
- `sync.output_dir`
- `gitea.token_env` or another token source

Keeping the token in an environment variable is recommended.

## Run

```bash
python gitea_repository_mirror.py
```

See `wiki.md` for configuration details.
