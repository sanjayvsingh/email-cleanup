# Email Cleanup Project — s@sanvash.com

## Goal
Build a Python script that connects directly to an IMAP email server and
cleans up marketing/promotional emails from the Inbox and Deleted Items
folders using a two-pass approach: a fast keyword pre-filter followed by
AI classification via the Anthropic API.

---

## Account & Scope
- Account: s@sanvash.com
- Connection: IMAP (credentials supplied at runtime or via .env file)
- Folders: Inbox and Deleted Items ONLY — do not touch any other folders
- Goal: Permanently delete marketing/promotional emails from both folders

---

## Credentials & API Key

### Anthropic API Key
- Do NOT prompt the user for an API key or create a separate subscription
- Read ANTHROPIC_API_KEY from the environment using os.environ
- Claude Code already has this set — the script inherits it automatically
- If the variable is missing, print a clear message explaining the script
  must be run from within a Claude Code session where the key is available

### IMAP Credentials
- Never hardcode any credentials or account details in the script
- On first run, prompt the user for: IMAP server, IMAP port (default 993),
  username, and password
- Save IMAP_SERVER, IMAP_PORT, and IMAP_USERNAME to a local .env file
- Never save the password to any file — always prompt for it at runtime
- Load from .env if it exists, prompt for anything missing

### GitHub Safety — files that must never be committed
The .gitignore must exclude:
- .env (contains server and username)
- *_preview_*.txt (generated reports contain personal email data)
- __pycache__/ and *.pyc

---

## Two-Pass Approach

### Pass 1 — Keyword Pre-filter (fast, no API calls)
Flag emails where the sender address OR subject line matches any of these.
These are unambiguous — no human sends personal email from these addresses
or uses this legal opt-out boilerplate in real correspondence:

Sender address prefixes (match start of address before the @):
  noreply, no-reply, donotreply, do-not-reply,
  marketing, deals, offers, promotions

Subject line phrases:
  unsubscribe
  manage your preferences
  subscription preferences
  no longer wish to receive
  remove me from this list

All matching is case-insensitive.

### Pass 2 — AI Classification (Anthropic API)
For all emails NOT caught by Pass 1, send them to Claude for classification.
To keep API usage efficient:
- Batch up to 10 emails per API call
- Send per email: date, sender address, sender name, subject, and first
  500 characters of body text (plain text only, strip HTML tags)
- Ask Claude to classify each as MARKETING or KEEP, with a one-line reason
- Use model claude-haiku-4-5-20251001 — this is classification, not reasoning
- Add a 0.5s delay between batches to avoid rate limiting
- Emails Claude classifies as KEEP must never appear in the deletion list

---

## Preview Report
Before deleting anything, generate a preview report.

The report must show:
- Timestamp and account name at the top
- Per-folder summary lines, e.g.:
    Inbox:          deleting 43 of 1,204 emails
    Deleted Items:  deleting 57 of 892 emails
    Total:          deleting 100 of 2,096 emails
- Two labelled sections: Pass 1 (keyword matches) and Pass 2 (AI classified)
- For each flagged email: date, sender, subject, reason
  (Pass 1: which keyword matched; Pass 2: Claude's one-line reason)

Save the report to a timestamped file:
  email_cleanup_preview_YYYYMMDD_HHMMSS.txt

Print the summary lines to the terminal, then ask:
  "Delete X emails permanently? This cannot be undone. (yes/no): "
Only accept the exact word "yes" — anything else cancels with no deletion.

---

## Deletion
- After confirmation, permanently delete all flagged emails
- For IMAP, permanent delete = mark as \Deleted + EXPUNGE on the folder
  (this removes from the server, not just moves to trash)
- Show progress: "Deleting 3 of 47..."
- Print a final summary when complete

---

## Error Handling
- Handle IMAP connection failures gracefully with clear error messages
- If an API batch call fails, skip that batch, log which emails were
  skipped, and continue — do not abort the whole run
- If a folder is missing or empty, note it and continue

---

## Dependencies
- imaplib — IMAP connection (stdlib)
- email — message parsing (stdlib)
- anthropic — Claude API
- python-dotenv — .env file support
- Any other dependencies must be pip-installable

---

## Files to Create
- email_cleanup.py      — main script
- requirements.txt      — pip dependencies
- .env.example          — template with variable names only, no real values
- .gitignore            — excludes .env, preview report files, __pycache__
