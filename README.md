# Email Cleanup

Cleans marketing, promotional, and spam emails from an IMAP mailbox using a two-pass approach: a fast keyword pre-filter followed by AI classification via the Google Gemini API.

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure `.env`

Copy `.env.example` to `.env` and fill in your values:

```
IMAP_SERVER=mail.example.com
IMAP_PORT=993
IMAP_USERNAME=you@example.com
GOOGLE_API_KEY=your-google-api-key
```

Get a free Google API key at [aistudio.google.com](https://aistudio.google.com).

Set your IMAP password as an **OS environment variable** (not in `.env`):

```bat
set IMAP_PASSWORD=yourpassword
```

If `IMAP_PASSWORD` is not set, the script will prompt for it at runtime.

## Usage

```
python3 email_cleanup.py <folder> [--limit N]
python3 email_cleanup.py --list-folders
```

### Arguments

| Argument | Description |
|---|---|
| `folder` | IMAP folder to scan (required) |
| `--limit N` | Max unprocessed emails to fetch (default: 100) |
| `--list-folders` | Print all available IMAP folders and message counts, then exit |

### Examples

```bash
# See available folders with message counts
python3 email_cleanup.py --list-folders

# Test run — 100 oldest unprocessed emails from trash
python3 email_cleanup.py INBOX.Trash

# Full run — up to 20,000 emails
python3 email_cleanup.py INBOX.Trash --limit 20000

# Inbox
python3 email_cleanup.py INBOX --limit 20000
```

## How it works

**Pass 1 — Keyword filter (no API calls)**
Instantly flags emails where the sender address starts with `marketing`, `deals`, `offers`, or `promotions`, or where the subject contains opt-out boilerplate like "unsubscribe" or "manage your preferences".

**Pass 2 — AI classification (Gemini API)**
Remaining emails are classified in the background in batches of 10. Each email is classified as:
- `MARKETING` — promotional emails, newsletters, shipping notifications, review/feedback requests
- `SPAM` — phishing, scams, fraud, impersonation
- `KEEP` — personal correspondence, order confirmations with receipts, security alerts, password resets

When uncertain, the AI defaults to `KEEP`.

**Interactive chunk-by-chunk review**
Results are presented in chunks of 1,000 emails as they become available. For each chunk:
- A timestamped report is saved (`email_cleanup_preview_YYYYMMDD_HHMMSS_chunk{N}.txt`)
- A summary is printed showing keyword-flagged, AI marketing, and AI spam counts
- You type `yes`, `no`, or `quit` to delete, skip, or stop

The AI classifier runs in the background while you review and confirm earlier chunks, so it keeps working without waiting for you.

**Permanent deletion**
Flagged emails are marked `\Deleted` and `EXPUNGE`d — removed from the server, not moved to trash. A fresh IMAP connection is opened for each deletion batch, so long classification runs don't cause connection timeouts.

## Progress tracking

Processed emails are recorded in `processed_uids.json`. Subsequent runs automatically skip emails that have already been reviewed, so you can run incrementally without re-classifying the same emails. Pass 1 results are saved immediately; Pass 2 results are saved after each batch of 10, so progress is preserved even if the run is interrupted.

## Rate limits (free tier)

The script uses `gemma-3-27b-it` by default and falls back to `gemma-3-12b-it` when the quota is exhausted. Free tier limits per model:

- 30 requests/minute
- 15,000 input tokens/minute
- 14,400 requests/day

The script waits automatically when rate-limited and retries up to 5 times per batch. When both models are exhausted, remaining emails default to `KEEP`.
