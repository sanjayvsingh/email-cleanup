#!/usr/bin/env python3
"""Email cleanup: removes marketing/promotional emails via IMAP with AI classification."""

import argparse
import email as email_lib
import email.header
import email.utils
import getpass
import imaplib
import json
import os
import re
import time
from datetime import datetime
from html.parser import HTMLParser

from dotenv import load_dotenv
from google import genai

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PASS1_SENDER_PREFIXES = [
    'marketing', 'deals', 'offers', 'promotions',
]

PASS1_SUBJECT_PHRASES = [
    'unsubscribe',
    'manage your preferences',
    'subscription preferences',
    'no longer wish to receive',
    'remove me from this list',
]

TARGET_FOLDERS = ['INBOX', 'Deleted Items']
AI_BATCH_SIZE = 10
AI_MODELS = ['gemma-3-27b-it', 'gemma-3-12b-it']
BODY_MAX_CHARS = 500
BATCH_DELAY = 10
ENV_FILE = '.env'
PROCESSED_UIDS_FILE = 'processed_uids.json'


class QuotaExhausted(Exception):
    pass


def load_processed_uids():
    if not os.path.exists(PROCESSED_UIDS_FILE):
        return {}
    with open(PROCESSED_UIDS_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_processed_uids(processed):
    with open(PROCESSED_UIDS_FILE, 'w', encoding='utf-8') as f:
        json.dump(processed, f)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts = []

    def handle_data(self, data):
        self._parts.append(data)

    def get_text(self):
        return ' '.join(self._parts)


def strip_html(html_text):
    stripper = _HTMLStripper()
    try:
        stripper.feed(html_text)
    except Exception:
        pass
    return stripper.get_text()


def decode_header(value):
    if not value:
        return ''
    parts = email.header.decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or 'utf-8', errors='replace'))
        else:
            decoded.append(part)
    return ''.join(decoded)


def get_body(msg):
    body = ''
    if msg.is_multipart():
        for part in msg.walk():
            if 'attachment' in str(part.get('Content-Disposition', '')):
                continue
            ctype = part.get_content_type()
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or 'utf-8'
            text = payload.decode(charset, errors='replace')
            if ctype == 'text/plain':
                body = text
                break
            if ctype == 'text/html' and not body:
                body = strip_html(text)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or 'utf-8'
            text = payload.decode(charset, errors='replace')
            body = strip_html(text) if msg.get_content_type() == 'text/html' else text
    return body.strip()


def imap_quote(folder):
    """Quote IMAP folder name if it contains spaces."""
    return f'"{folder}"' if ' ' in folder else folder


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def _save_env_values(new_values):
    lines = []
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    for key, value in new_values.items():
        prefix = f'{key}='
        updated = False
        for i, line in enumerate(lines):
            if line.startswith(prefix):
                lines[i] = f'{key}={value}\n'
                updated = True
                break
        if not updated:
            lines.append(f'{key}={value}\n')
    with open(ENV_FILE, 'w', encoding='utf-8') as f:
        f.writelines(lines)


def load_credentials():
    # Read password from OS environment before load_dotenv so .env cannot shadow it
    password = os.environ.get('IMAP_PASSWORD', '').strip()

    load_dotenv(ENV_FILE)
    server = os.environ.get('IMAP_SERVER', '').strip()
    port_str = os.environ.get('IMAP_PORT', '').strip()
    username = os.environ.get('IMAP_USERNAME', '').strip()

    new_values = {}
    if not server:
        server = input('IMAP server (e.g. mail.example.com): ').strip()
        new_values['IMAP_SERVER'] = server
    if not port_str:
        port_str = input('IMAP port [993]: ').strip() or '993'
        new_values['IMAP_PORT'] = port_str
    if not username:
        username = input('IMAP username: ').strip()
        new_values['IMAP_USERNAME'] = username

    if new_values:
        _save_env_values(new_values)

    port = int(port_str)
    if not password:
        password = getpass.getpass(f'Password for {username}: ')
    return server, port, username, password


# ---------------------------------------------------------------------------
# IMAP
# ---------------------------------------------------------------------------

def connect_imap(server, port, username, password):
    try:
        mail = imaplib.IMAP4_SSL(server, port)
        mail.login(username, password)
        return mail
    except imaplib.IMAP4.error as e:
        raise ConnectionError(f'IMAP connection/login failed: {e}')


def fetch_emails(mail, folder, limit=None, skip_uids=None):
    """Fetch emails from a folder, skipping already-processed UIDs."""
    try:
        status, _ = mail.select(imap_quote(folder), readonly=True)
        if status != 'OK':
            print(f'  Folder "{folder}" not found — skipping.')
            return []
    except imaplib.IMAP4.error as e:
        print(f'  Error selecting "{folder}": {e} — skipping.')
        return []

    status, data = mail.uid('search', None, 'ALL')
    if status != 'OK' or not data[0]:
        print(f'  Folder "{folder}" is empty.')
        return []

    uids = data[0].split()
    if not uids:
        print(f'  Folder "{folder}" is empty.')
        return []

    if skip_uids:
        uids = [uid for uid in uids if uid.decode() not in skip_uids]

    if limit is not None:
        uids = uids[:limit]

    print(f'  Fetching {len(uids)} unprocessed emails from "{folder}"...')
    results = []
    for uid in uids:
        try:
            status, msg_data = mail.uid('fetch', uid, '(RFC822)')
            if status != 'OK' or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            msg = email_lib.message_from_bytes(raw)

            sender_full = decode_header(msg.get('From', ''))
            name, addr = email.utils.parseaddr(sender_full)

            results.append({
                'uid': uid,
                'msg': msg,
                'date': decode_header(msg.get('Date', '')),
                'sender_addr': addr.lower().strip(),
                'sender_name': name,
                'subject': decode_header(msg.get('Subject', '(no subject)')),
                'folder': folder,
            })
        except Exception as e:
            print(f'  Warning: skipping uid {uid}: {e}')

    return results


# ---------------------------------------------------------------------------
# Pass 1 — Keyword filter
# ---------------------------------------------------------------------------

def pass1_check(entry):
    local = entry['sender_addr'].split('@')[0] if '@' in entry['sender_addr'] else entry['sender_addr']
    for prefix in PASS1_SENDER_PREFIXES:
        if local.startswith(prefix):
            return f'sender prefix: {prefix}'

    subject_lower = entry['subject'].lower()
    for phrase in PASS1_SUBJECT_PHRASES:
        if phrase in subject_lower:
            return f'subject phrase: "{phrase}"'

    return None


# ---------------------------------------------------------------------------
# Pass 2 — AI classification
# ---------------------------------------------------------------------------

def _format_batch_prompt(batch):
    items = []
    for i, entry in enumerate(batch):
        body = get_body(entry['msg'])
        snippet = body[:BODY_MAX_CHARS] if body else '(no body)'
        items.append(
            f"Email {i + 1}:\n"
            f"  Date: {entry['date']}\n"
            f"  From: {entry['sender_name']} <{entry['sender_addr']}>\n"
            f"  Subject: {entry['subject']}\n"
            f"  Body: {snippet}"
        )
    return (
        "Classify each email as MARKETING, SPAM, or KEEP.\n"
        "MARKETING = promotional emails, newsletters, advertising, sales, shipping notifications, "
        "delivery updates, review/feedback requests, post-purchase surveys, subscription updates, "
        "and any automated message that is not a record of a transaction.\n"
        "SPAM = phishing, scams, fraud, fake prizes, impersonation, suspicious links, malware.\n"
        "KEEP = personal correspondence, security alerts, password resets, account notices, "
        "and ORDER CONFIRMATIONS that serve as a receipt (contain an order number, itemised "
        "purchase, or payment confirmation). Shipping notifications and review requests are NOT receipts — classify those as MARKETING.\n\n"
        "When uncertain, always classify as KEEP — it is better to keep an email than to delete something important.\n\n"
        "Respond with exactly one line per email:\n"
        "Email N: MARKETING|SPAM|KEEP — <one-line reason>\n\n"
        + '\n\n'.join(items)
    )


def classify_batch(client, model, batch):
    max_retries = 5
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(model=model, contents=_format_batch_prompt(batch))
            text = response.text
            results = []
            for i in range(len(batch)):
                m = re.search(rf'Email {i + 1}:\s*(MARKETING|SPAM|KEEP)\s*[—\-–]\s*(.+)', text, re.IGNORECASE)
                if m:
                    results.append((m.group(1).upper(), m.group(2).strip()))
                else:
                    results.append(('KEEP', 'could not parse AI response — defaulting to KEEP'))
            return results
        except Exception as e:
            err_str = str(e)
            if '429' in err_str or 'RESOURCE_EXHAUSTED' in err_str:
                if attempt < max_retries - 1:
                    delay_match = re.search(r'retryDelay[^\d]*(\d+)s', err_str)
                    wait = int(delay_match.group(1)) + 5 if delay_match else 65
                    print(f'  Rate limit on {model} — waiting {wait}s then retrying (attempt {attempt + 1}/{max_retries})...')
                    time.sleep(wait)
                    continue
                raise QuotaExhausted(f'{model} quota exhausted after {max_retries} retries')
            print(f'  API batch error: {e} — skipping batch, all defaulting to KEEP')
            return [('KEEP', f'API error: {e}')] * len(batch)
    raise QuotaExhausted(f'{model} max retries exceeded')


def pass2_classify(client, candidates):
    batches = [candidates[i:i + AI_BATCH_SIZE] for i in range(0, len(candidates), AI_BATCH_SIZE)]
    results = []
    model_idx = 0
    consecutive_failures = 0

    for idx, batch in enumerate(batches):
        model = AI_MODELS[model_idx % len(AI_MODELS)]
        print(f'  AI classifying batch {idx + 1}/{len(batches)} ({len(batch)} emails) [{model}]...')

        while True:
            try:
                classifications = classify_batch(client, model, batch)
                consecutive_failures = 0
                break
            except QuotaExhausted:
                consecutive_failures += 1
                if consecutive_failures >= len(AI_MODELS):
                    print('  All models quota exhausted — defaulting remaining emails to KEEP.')
                    for remaining in batches[idx:]:
                        for entry in remaining:
                            entry['classification'] = 'KEEP'
                            entry['reason'] = 'all models quota exhausted'
                            results.append(entry)
                    return results
                model_idx = (model_idx + 1) % len(AI_MODELS)
                model = AI_MODELS[model_idx]
                print(f'  Switching to {model}...')

        for entry, (classification, reason) in zip(batch, classifications):
            entry['classification'] = classification
            entry['reason'] = reason
            results.append(entry)

        if idx < len(batches) - 1:
            time.sleep(BATCH_DELAY)

    return results


# ---------------------------------------------------------------------------
# Preview report
# ---------------------------------------------------------------------------

def build_report(pass1_flagged, pass2_results, all_counts, now_str, folders=None):
    if folders is None:
        folders = TARGET_FOLDERS
    p2_marketing = [e for e in pass2_results if e['classification'] == 'MARKETING']
    p2_spam = [e for e in pass2_results if e['classification'] == 'SPAM']
    p2_to_delete = p2_marketing + p2_spam
    total_flagged = len(pass1_flagged) + len(p2_to_delete)
    total_emails = sum(all_counts.values())

    lines = [
        'Email Cleanup Preview Report',
        f'Generated: {now_str}',
        f'Account:   {os.environ.get("IMAP_USERNAME", "unknown")}',
        '',
    ]

    for folder in folders:
        f_p1 = sum(1 for e in pass1_flagged if e['folder'] == folder)
        f_p2 = sum(1 for e in p2_to_delete if e['folder'] == folder)
        lines.append(f'{folder + ":":<22} deleting {f_p1 + f_p2:,} of {all_counts.get(folder, 0):,} emails')

    lines += [
        f'{"Total:":<22} deleting {total_flagged:,} of {total_emails:,} emails',
        '',
        '=' * 64,
        f'PASS 1 — Keyword Matches ({len(pass1_flagged)} emails)',
        '=' * 64,
    ]
    for e in pass1_flagged:
        lines += [
            f'  [{e["folder"]}] {e["date"]}',
            f'  From:    {e["sender_name"]} <{e["sender_addr"]}>',
            f'  Subject: {e["subject"]}',
            f'  Reason:  {e["reason"]}',
            '',
        ]

    lines += [
        '=' * 64,
        f'PASS 2 — AI: Marketing ({len(p2_marketing)} emails)',
        '=' * 64,
    ]
    for e in p2_marketing:
        lines += [
            f'  [{e["folder"]}] {e["date"]}',
            f'  From:    {e["sender_name"]} <{e["sender_addr"]}>',
            f'  Subject: {e["subject"]}',
            f'  Reason:  {e["reason"]}',
            '',
        ]

    lines += [
        '=' * 64,
        f'PASS 2 — AI: Spam ({len(p2_spam)} emails)',
        '=' * 64,
    ]
    for e in p2_spam:
        lines += [
            f'  [{e["folder"]}] {e["date"]}',
            f'  From:    {e["sender_name"]} <{e["sender_addr"]}>',
            f'  Subject: {e["subject"]}',
            f'  Reason:  {e["reason"]}',
            '',
        ]

    return '\n'.join(lines), total_flagged, p2_to_delete


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

def delete_emails(mail, to_delete):
    by_folder = {}
    for e in to_delete:
        by_folder.setdefault(e['folder'], []).append(e['uid'])

    total = len(to_delete)
    deleted = 0

    for folder, uids in by_folder.items():
        try:
            status, _ = mail.select(imap_quote(folder))
            if status != 'OK':
                print(f'  Could not select "{folder}" for deletion — skipping {len(uids)} emails.')
                continue
            for uid in uids:
                deleted += 1
                mail.uid('store', uid, '+FLAGS', r'(\Deleted)')
                if deleted % 10 == 0:
                    print('.', end='', flush=True)
            mail.expunge()
            print()
        except imaplib.IMAP4.error as e:
            print(f'\n  Error during deletion in "{folder}": {e}')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    load_dotenv(ENV_FILE)

    parser = argparse.ArgumentParser(description='Email cleanup tool')
    parser.add_argument('folder', nargs='?', help='Folder to scan (omit to scan all target folders)')
    parser.add_argument('--list-folders', action='store_true', help='List available IMAP folders and exit')
    parser.add_argument('--limit', type=int, default=100, help='Max unprocessed emails to fetch per folder (default: 100)')
    args = parser.parse_args()

    api_key = os.environ.get('GOOGLE_API_KEY')
    if not api_key:
        print('Error: GOOGLE_API_KEY is not set.\nAdd it to your .env file:  GOOGLE_API_KEY=...')
        return

    print('=== Email Cleanup ===\n')

    server, port, username, password = load_credentials()

    print(f'\nConnecting to {server}:{port}...')
    try:
        mail = connect_imap(server, port, username, password)
    except ConnectionError as e:
        print(f'Error: {e}')
        return
    print('Connected.\n')

    if not args.folder and not args.list_folders:
        parser.print_help()
        return

    if args.list_folders:
        print('Available folders:')
        _, folder_list = mail.list()
        for item in folder_list or []:
            print(' ', item.decode(errors='replace'))
        mail.logout()
        return

    folders_to_scan = [args.folder] if args.folder else TARGET_FOLDERS
    processed = load_processed_uids()

    # Fetch
    all_emails = []
    all_counts = {folder: 0 for folder in folders_to_scan}
    for folder in folders_to_scan:
        print(f'Scanning folder: {folder}')
        skip = set(processed.get(folder, []))
        emails = fetch_emails(mail, folder, limit=args.limit, skip_uids=skip)
        all_counts[folder] = len(emails)
        all_emails.extend(emails)
        print()

    if not all_emails:
        print('No emails found. Exiting.')
        mail.logout()
        return

    # Pass 1
    print('Pass 1: Keyword filtering...')
    pass1_flagged, pass2_candidates = [], []
    for e in all_emails:
        reason = pass1_check(e)
        if reason:
            e['reason'] = reason
            pass1_flagged.append(e)
        else:
            pass2_candidates.append(e)
    print(f'  Flagged: {len(pass1_flagged)}   Candidates for AI: {len(pass2_candidates)}\n')

    # Pass 2
    client = genai.Client(api_key=api_key)
    pass2_results = []
    if pass2_candidates:
        print('Pass 2: AI classification...')
        pass2_results = pass2_classify(client, pass2_candidates)
        ai_flagged = sum(1 for e in pass2_results if e['classification'] in ('MARKETING', 'SPAM'))
        print(f'  AI flagged: {ai_flagged} additional emails\n')

    # Report
    now = datetime.now()
    report_text, total_flagged, p2_to_delete = build_report(
        pass1_flagged, pass2_results, all_counts, now.strftime('%Y-%m-%d %H:%M:%S'),
        folders=folders_to_scan,
    )
    report_file = f'email_cleanup_preview_{now.strftime("%Y%m%d_%H%M%S")}.txt'
    with open(report_file, 'w', encoding='utf-8') as f:
        f.write(report_text)
    print(f'Preview report saved to: {report_file}\n')

    # Print summary
    for folder in folders_to_scan:
        f_p1 = sum(1 for e in pass1_flagged if e['folder'] == folder)
        f_p2 = sum(1 for e in p2_to_delete if e['folder'] == folder)
        print(f'{folder + ":":<22} deleting {f_p1 + f_p2:,} of {all_counts.get(folder, 0):,} emails')
    total_emails = sum(all_counts.values())
    print(f'{"Total:":<22} deleting {total_flagged:,} of {total_emails:,} emails\n')

    if total_flagged == 0:
        print('Nothing to delete. Exiting.')
        mail.logout()
        return

    # Mark all reviewed emails as processed so they are skipped on future runs
    for e in all_emails:
        uid_str = e['uid'].decode() if isinstance(e['uid'], bytes) else str(e['uid'])
        processed.setdefault(e['folder'], [])
        if uid_str not in processed[e['folder']]:
            processed[e['folder']].append(uid_str)
    save_processed_uids(processed)

    answer = input(f'Delete {total_flagged} emails permanently? This cannot be undone. (yes/no): ').strip()
    if answer != 'yes':
        print('Cancelled. No emails were deleted.')
        mail.logout()
        return

    to_delete = pass1_flagged + p2_to_delete
    print(f'\nDeleting {total_flagged} emails...')
    delete_emails(mail, to_delete)
    print(f'\nDone. {total_flagged} emails permanently deleted.')
    mail.logout()


if __name__ == '__main__':
    main()
