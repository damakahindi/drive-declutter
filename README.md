# Drive Declutter

A local web tool that finds large, duplicate and stale files in your Google Drive, shows what a cheaper
setup (smaller Google One plan + external hard disks) would cost, and lets you archive files to a disk
before trashing them.

## Safety
- Runs only on `127.0.0.1`; your token stays in `token.json` (gitignored).
- Only files **you own** are listed. Nothing is permanently deleted — files go to Drive's trash (30 days).
- "Archive & trash" downloads each file, verifies its MD5 against Drive, and only then trashes it.
- Google Docs/Sheets/Slides take no quota and are ignored.

## Setup
1. In [Google Cloud Console](https://console.cloud.google.com/), create a project, enable the **Google Drive API**.
2. Configure the OAuth consent screen (External, add yourself as a test user).
3. Create an **OAuth client ID** of type *Web application* with redirect URI `http://localhost:5000/oauth2callback`.
4. Download the JSON as `credentials.json` into this folder.
5. Run:
   ```
   pip install -r requirements.txt
   python app.py
   ```
6. Open http://localhost:5000, sign in, click **Scan Drive**.

## Cost advice
`costs.py` holds approximate Google One tiers and external-disk prices. Edit them to match your region.
The advisor budgets two disks (working copy + backup) and compares 5-year cost.

## Roadmap
- Folder-level size breakdown
- Scheduled scans
- Optional cloud-offsite backup tier (e.g. Backblaze B2) in the cost model
