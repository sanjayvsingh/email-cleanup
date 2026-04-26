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
import queue
import re
import threading
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
IMAP_FETCH_BATCH = 100
INTERACTIVE_CHUNK_SIZE = 1000
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


def _safe_decode(payload, charset):
    """Decode bytes, falling back to latin-1 for unknown or invalid charsets."""
    try:
        return payload.decode(charset, errors='replace')
    except (LookupError, UnicodeDecodeError):
        return payload.decode('latin-1', errors='replace')


def decode_header(value):
    if not value:
        return ''
    parts = email.header.decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(_safe_decode(part, charset or 'utf-8'))
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
            text = _safe_decode(payload, part.get_content_charset() or 'utf-8')
            if ctype == 'text/plain':
                body = text
                break
            if ctype == 'text/html' and not body:
                body = strip_html(text)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            text = _safe_decode(payload, msg.get_content_charset() or 'utf-8')
            body = strip_html(text) if msg.get_content_type() == 'text/html' else text
    return body.strip()


def imap_quote(folder):
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
    # Read password from OS env before load_dotenv so .env cannot shadow it
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


def get_folder_message_count(mail, folder):
    """Return total message count via STATUS — does not select the folder."""
    try:
        status, data = mail.status(imap_quote(folder), '(MESSAGES)')
        if status == 'OK' and data:
            m = re.search(rb'MESSAGES (\d+)', data[0])
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return None


def fetch_emails(mail, folder, limit=None, skip_uids=None):
    """Fetch emails in IMAP batches, skipping already-processed UIDs."""
    try:
        status, data = mail.select(imap_quote(folder), readonly=True)
        if status != 'OK':
            print(f'  Folder "{folder}" not found — skipping.')
            return []
        total_in_folder = int(data[0]) if data and data[0] else 0
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

    fetch_count = min(limit, len(uids)) if limit is not None else len(uids)
    uids = uids[:fetch_count]

    print(f'  {total_in_folder:,} total  |  {fetch_count:,} unprocessed to fetch')

    results = []
    for i in range(0, len(uids), IMAP_FETCH_BATCH):
        batch_uids = uids[i:i + IMAP_FETCH_BATCH]
        uid_list = b','.join(batch_uids)
        try:
            status, msg_data = mail.uid('fetch', uid_list, '(UID RFC822)')
            if status != 'OK' or not msg_data:
                continue
            for item in msg_data:
                if not isinstance(item, tuple) or len(item) < 2 or not isinstance(item[1], bytes):
                    continue
                header_bytes, raw = item[0], item[1]
                try:
                    uid_match = re.search(rb'\bUID\s+(\d+)', header_bytes)
                    uid = uid_match.group(1) if uid_match else None
                    if uid is None:
                        continue
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
                    print(f'  Warning: skipping a message: {e}')
        except imaplib.IMAP4.error as e:
            print(f'  Warning: batch fetch error at offset {i}: {e}')

        fetched = min(i + IMAP_FETCH_BATCH, len(uids))
        if fetched % 500 == 0 or fetched == len(uids):
            print(f'  ...{fetched:,} of {fetch_count:,} fetched')

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
# Pass 2 — AI classification (background thread)
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
        "purchase, or payment confirmation). Shipping notifications and review requests are NOT "
        "receipts — classify those as MARKETING.\n\n"
        "When uncertain, always classify as KEEP — it is better to keep an email than to delete "
        "something important.\n\n"
        "Respond with exactly one line per email:\n"
        "Email N: MARKETING|SPAM|KEEP — <one-line reason>\n\n"
        + '\n\n'.join(items)
    )


def classify_batch(client, model, batch):
    max_retries = 5
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=model, contents=_format_batch_prompt(batch)
            )
            text = response.text
            results = []
            for i in range(len(batch)):
                m = re.search(
                    rf'Email {i + 1}:\s*(MARKETING|SPAM|KEEP)\s*[—\-–]\s*(.+)',
                    text, re.IGNORECASE
                )
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
                    print(f'\n  Rate limit on {model} — waiting {wait}s '
                          f'(attempt {attempt + 1}/{max_retries})...')
                    time.sleep(wait)
                    continue
                raise QuotaExhausted(f'{model} quota exhausted after {max_retries} retries')
            print(f'\n  API batch error: {e} — skipping batch, defaulting to KEEP')
            return [('KEEP', f'API error: {e}')] * len(batch)
    raise QuotaExhausted(f'{model} max retries exceeded')


def classification_worker(client, candidates, result_queue, initial_processed, quiet):
    """Background thread: classifies emails and saves progress after every batch."""
    processed = {k: list(v) for k, v in initial_processed.items()}
    model_idx = 0
    consecutive_failures = 0
    batches = [candidates[i:i + AI_BATCH_SIZE] for i in range(0, len(candidates), AI_BATCH_SIZE)]
    total_batches = len(batches)

    for idx, batch in enumerate(batches):
        model = AI_MODELS[model_idx % len(AI_MODELS)]
        if not quiet.is_set():
            print(f'  [classifier] batch {idx + 1}/{total_batches} [{model}]')

        while True:
            try:
                classifications = classify_batch(client, model, batch)
                consecutive_failures = 0
                break
            except QuotaExhausted:
                consecutive_failures += 1
                if consecutive_failures >= len(AI_MODELS):
                    print('\n  All models quota exhausted — remaining emails default to KEEP.')
                    for remaining_batch in batches[idx:]:
                        for entry in remaining_batch:
                            entry['classification'] = 'KEEP'
                            entry['reason'] = 'all models quota exhausted'
                            result_queue.put(entry)
                    result_queue.put(None)
                    return
                model_idx = (model_idx + 1) % len(AI_MODELS)
                model = AI_MODELS[model_idx]
                print(f'  Switching to {model}...')

        for entry, (classification, reason) in zip(batch, classifications):
            entry['classification'] = classification
            entry['reason'] = reason
            result_queue.put(entry)
            uid_str = entry['uid'].decode() if isinstance(entry['uid'], bytes) else str(entry['uid'])
            processed.setdefault(entry['folder'], [])
            if uid_str not in processed[entry['folder']]:
                processed[entry['folder']].append(uid_str)

        save_processed_uids(processed)

        if idx < total_batches - 1:
            time.sleep(BATCH_DELAY)

    result_queue.put(None)


# ---------------------------------------------------------------------------
# Report (per chunk)
# ---------------------------------------------------------------------------

def build_chunk_report(chunk, chunk_num, now_str):
    p1 = [e for e in chunk if e.get('pass') == 1]
    p2_marketing = [e for e in chunk if e.get('pass') == 2 and e['classification'] == 'MARKETING']
    p2_spam = [e for e in chunk if e.get('pass') == 2 and e['classification'] == 'SPAM']
    to_delete = p1 + p2_marketing + p2_spam

    lines = [
        f'Email Cleanup — Chunk {chunk_num}',
        f'Generated: {now_str}',
        f'Account:   {os.environ.get("IMAP_USERNAME", "unknown")}',
        f'Reviewed: {len(chunk):,}   Flagged: {len(to_delete):,}',
        '',
        '=' * 64,
        f'PASS 1 — Keyword Matches ({len(p1)} emails)',
        '=' * 64,
    ]
    for e in p1:
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

    return '\n'.join(lines), to_delete


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

def delete_emails(to_delete, server, port, username, password):
    by_folder = {}
    for e in to_delete:
        by_folder.setdefault(e['folder'], []).append(e['uid'])

    deleted = 0

    try:
        mail = connect_imap(server, port, username, password)
    except ConnectionError as e:
        print(f'  Could not reconnect for deletion: {e}')
        return

    for folder, uids in by_folder.items():
        try:
            status, _ = mail.select(imap_quote(folder))
            if status != 'OK':
                print(f'  Could not select "{folder}" — skipping {len(uids)} emails.')
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

    try:
        mail.logout()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    load_dotenv(ENV_FILE)

    parser = argparse.ArgumentParser(description='Email cleanup tool')
    parser.add_argument('folder', nargs='?', help='IMAP folder to scan')
    parser.add_argument('--list-folders', action='store_true',
                        help='List available folders with message counts and exit')
    parser.add_argument('--limit', type=int, default=100,
                        help='Max unprocessed emails to fetch (default: 100)')
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
        mail.logout()
        return

    if args.list_folders:
        print(f'{"Messages":>10}  Folder')
        print('-' * 40)
        _, folder_list = mail.list()
        for item in folder_list or []:
            decoded = item.decode(errors='replace')
            m = re.match(r'\([^)]*\)\s+"[^"]*"\s+"?(.+?)"?\s*$', decoded)
            if m:
                fname = m.group(1).strip()
                count = get_folder_message_count(mail, fname)
                count_str = f'{count:>10,}' if count is not None else f'{"?":>10}'
                print(f'{count_str}  {fname}')
            else:
                print(f'{"?":>10}  {decoded}')
        mail.logout()
        return

    folders_to_scan = [args.folder] if args.folder else TARGET_FOLDERS
    processed = load_processed_uids()

    # Fetch
    all_emails = []
    for folder in folders_to_scan:
        print(f'Scanning folder: {folder}')
        skip = set(processed.get(folder, []))
        emails = fetch_emails(mail, folder, limit=args.limit, skip_uids=skip)
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
            e['pass'] = 1
            pass1_flagged.append(e)
        else:
            pass2_candidates.append(e)
    print(f'  Keyword flagged: {len(pass1_flagged):,}   AI candidates: {len(pass2_candidates):,}\n')

    # Save pass1 UIDs immediately — they're fully processed regardless of deletion outcome
    for e in pass1_flagged:
        uid_str = e['uid'].decode() if isinstance(e['uid'], bytes) else str(e['uid'])
        processed.setdefault(e['folder'], [])
        if uid_str not in processed[e['folder']]:
            processed[e['folder']].append(uid_str)
    save_processed_uids(processed)

    # Start background classifier
    result_queue = queue.Queue()
    client = genai.Client(api_key=api_key)

    if pass2_candidates:
        print('Pass 2: Starting AI classification in background...\n')
        worker = threading.Thread(
            target=classification_worker,
            args=(client, pass2_candidates, result_queue, processed, quiet),
            daemon=True,
        )
        worker.start()
    else:
        result_queue.put(None)
        worker = None

    # Interactive chunk loop
    pass1_idx = 0
    chunk_num = 0
    worker_done = not bool(pass2_candidates)
    total_deleted = 0
    last_item_time = time.time()
    quiet = threading.Event()

    while pass1_idx < len(pass1_flagged) or not worker_done:
        chunk = []

        # Fill from pass1 first
        while pass1_idx < len(pass1_flagged) and len(chunk) < INTERACTIVE_CHUNK_SIZE:
            chunk.append(pass1_flagged[pass1_idx])
            pass1_idx += 1

        # Fill remainder from AI queue
        while len(chunk) < INTERACTIVE_CHUNK_SIZE and not worker_done:
            try:
                item = result_queue.get(timeout=2)
                if item is None:
                    worker_done = True
                else:
                    item['pass'] = 2
                    chunk.append(item)
                    last_item_time = time.time()
            except queue.Empty:
                # If we have items and the worker has been quiet for >60s
                # (e.g. waiting out a rate-limit retry), present what we have
                if chunk and (time.time() - last_item_time) > 60:
                    break

        if not chunk:
            break

        chunk_num += 1
        now = datetime.now()
        report_text, to_delete = build_chunk_report(
            chunk, chunk_num, now.strftime('%Y-%m-%d %H:%M:%S')
        )

        report_file = f'email_cleanup_preview_{now.strftime("%Y%m%d_%H%M%S")}_chunk{chunk_num}.txt'
        with open(report_file, 'w', encoding='utf-8') as f:
            f.write(report_text)

        print(f'\n{"=" * 64}')
        print(f'Chunk {chunk_num} ready — {len(chunk):,} emails reviewed')
        print(f'  Keyword flagged:  {sum(1 for e in chunk if e.get("pass") == 1):,}')
        print(f'  AI marketing:     {sum(1 for e in chunk if e.get("pass") == 2 and e["classification"] == "MARKETING"):,}')
        print(f'  AI spam:          {sum(1 for e in chunk if e.get("pass") == 2 and e["classification"] == "SPAM"):,}')
        print(f'  Flagged total:    {len(to_delete):,}')
        print(f'  Report saved to:  {report_file}')

        if not to_delete:
            print('  Nothing to delete in this chunk.')
            continue

        quiet.set()
        answer = input(
            f'\nDelete {len(to_delete):,} emails permanently? (yes / no / quit): '
        ).strip().lower()
        quiet.clear()

        if answer == 'quit':
            print('Stopping. Progress saved — remaining emails will be picked up next run.')
            if worker:
                worker.join(timeout=2)
            mail.logout()
            return
        elif answer == 'yes':
            print(f'Deleting {len(to_delete):,} emails...')
            delete_emails(to_delete, server, port, username, password)
            total_deleted += len(to_delete)
            print(f'  Chunk {chunk_num} done. Running total: {total_deleted:,} deleted.')
        else:
            print('  Skipped — no emails deleted for this chunk.')

    if worker:
        worker.join(timeout=5)

    print(f'\n{"=" * 64}')
    print(f'All done. {total_deleted:,} emails permanently deleted.')
    mail.logout()


if __name__ == '__main__':
    main()
