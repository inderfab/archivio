"""Mail-Scanner: IMAP → Archivio DB.

Nur lesend: IMAP SELECT + FETCH (PEEK), kein STORE/DELETE/EXPUNGE.
"""
from __future__ import annotations

import base64
import imaplib
import json
import logging
import re
from email import message_from_bytes
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

from config import settings
from db import connection

log = logging.getLogger(__name__)


# ── IMAP-Verbindung ───────────────────────────────────────────────────────────

def connect_imap(password: str | None = None) -> imaplib.IMAP4_SSL:
    host     = settings.get("mail.host",     "imap.mail.hostpoint.ch")
    port     = int(settings.get("mail.port", 993))
    username = settings.get("mail.username", "")
    pw       = password or settings.get("mail.password", "")
    client   = imaplib.IMAP4_SSL(host, port)
    client.login(username, pw)
    log.info("IMAP verbunden: %s@%s", username, host)
    return client


# ── Postfächer auflisten ──────────────────────────────────────────────────────

def list_mailboxes(client: imaplib.IMAP4_SSL) -> list[str]:
    status, raw_list = client.list()
    if status != "OK" or raw_list is None:
        raise RuntimeError("Postfächer konnten nicht aufgelistet werden.")
    result = []
    for raw_line in raw_list:
        raw_name  = _parse_mailbox_name(raw_line)
        decoded   = _decode_imap_utf7(raw_name)
        result.append(decoded)
    return sorted(result)


def _parse_mailbox_name(raw_line: bytes) -> str:
    text = raw_line.decode("utf-8", errors="replace").strip()
    match = re.search(r'"([^"]+)"\s*$', text)
    if match:
        return match.group(1)
    parts = text.split(' "/" ', 1)
    if len(parts) == 2:
        return parts[1].strip().strip('"')
    return text.rsplit(" ", 1)[-1].strip().strip('"')


def _decode_imap_utf7(text: str) -> str:
    def repl(m):
        token = m.group(1)
        if token == "":
            return "&"
        token   = token.replace(",", "/")
        padding = "=" * (-len(token) % 4)
        data    = base64.b64decode(token + padding)
        return data.decode("utf-16-be")
    return re.sub(r"&([^-]*)-", repl, text)


# ── Projekt-Matching ──────────────────────────────────────────────────────────

def match_mailbox_to_project(conn, mailbox_name: str) -> int | None:
    """Postfachname enthält Projektnummer → project_id oder None."""
    m = re.search(r'\d{3,}', mailbox_name)
    if not m:
        return None
    number = m.group(0)
    row = conn.execute(
        "SELECT id FROM projects WHERE path LIKE ?",
        (f"%{number}%",),
    ).fetchone()
    return row["id"] if row else None


# ── Mail-Parser ───────────────────────────────────────────────────────────────

def decode_mime_header(value) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def split_addresses(header: str) -> tuple[str, str]:
    addresses = getaddresses([header or ""])
    names, emails = [], []
    for name, addr in addresses:
        if name:
            names.append(decode_mime_header(name))
        if addr:
            emails.append(addr)
    return ", ".join(names), ", ".join(emails)


def parse_mail_date(date_header: str) -> str:
    try:
        return parsedate_to_datetime(date_header).isoformat()
    except Exception:
        return ""


def extract_text_part(message) -> str:
    text = ""
    if message.is_multipart():
        for part in message.walk():
            disp = part.get("Content-Disposition", "")
            if disp and "attachment" in disp:
                continue
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    text += payload.decode(charset, errors="replace")
    else:
        payload = message.get_payload(decode=True)
        if payload:
            charset = message.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
    return _normalize_whitespace(text)


def extract_attachments_metadata(message) -> list[dict]:
    attachments = []
    for part in message.walk():
        filename = part.get_filename()
        if not filename:
            continue
        fname    = decode_mime_header(filename)
        ext      = Path(fname).suffix.lower()
        payload  = part.get_payload(decode=True)
        size     = len(payload) if payload else 0
        attachments.append({"name": fname, "extension": ext, "size": size})
    return attachments


def build_email_record(message, mailbox: str) -> dict:
    sender_name, sender_email = split_addresses(message.get("From", ""))
    _, to_emails   = split_addresses(message.get("To",  ""))
    _, cc_emails   = split_addresses(message.get("Cc",  ""))
    subject        = decode_mime_header(message.get("Subject", ""))
    message_id     = (message.get("Message-ID")  or "").strip()
    in_reply_to    = (message.get("In-Reply-To") or "").strip()
    raw_text       = extract_text_part(message)
    attachments    = extract_attachments_metadata(message)

    sender = f"{sender_name} <{sender_email}>".strip(" <>") if sender_email else sender_name

    return {
        "message_id":   message_id,
        "mail_date":    parse_mail_date(message.get("Date", "")),
        "sender":       sender,
        "recipients":   to_emails,
        "cc":           cc_emails,
        "subject":      subject,
        "raw_text":     raw_text,
        "cleaned_text": clean_mail_text(raw_text),
        "attachments":  attachments,
        "thread_id":    in_reply_to,
        "mailbox":      mailbox,
    }


# ── Text-Bereinigung ──────────────────────────────────────────────────────────

def _normalize_whitespace(text: str) -> str:
    text = text.replace("\r", "\n").replace(" ", " ").replace("￼", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _strip_reply_chain(text: str) -> str:
    patterns = [
        r"\nOn .* wrote:", r"\nAm .* schrieb", r"\nVon:",
        r"\nBegin forwarded message",
        r"\n---------- Forwarded message ---------",
        r"\n--- Original Message ---",
        r"\n>\s*Am .* schrieb", r"\n>\s*On .* wrote:",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            text = text[:m.start()]
            break
    cleaned = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(">"):
            continue
        if re.match(
            r"^(Am .+ schrieb.*:|On .+ wrote:|Von:|From:|Gesendet:|Sent:|An:|To:|Cc:|Betreff:|Subject:)",
            s, re.IGNORECASE,
        ):
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def _strip_signature(text: str) -> str:
    markers = [
        "\nFreundliche Grüsse", "\nFreundliche Gruesse",
        "\nHerzliche Grüsse",   "\nHerzliche Gruesse",
        "\nLiebe Grüsse",       "\nLiebe Gruesse",
        "\nKind regards",       "\nBest regards",
        "\nRegards",            "\nSent from my",
    ]
    lower = text.lower()
    for marker in markers:
        pos = lower.find(marker.lower())
        if pos != -1:
            text = text[:pos]
            break
    return text.strip()


def clean_mail_text(raw: str) -> str:
    text = _normalize_whitespace(raw)
    text = _strip_reply_chain(text)
    text = _strip_signature(text)
    return _normalize_whitespace(text)


# ── DB-Schreiber ──────────────────────────────────────────────────────────────

def mail_exists(conn, message_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM documents WHERE hash=? AND source_type='email'",
        (message_id,),
    ).fetchone() is not None


def save_mail_to_db(conn, record: dict, project_id: int) -> bool:
    """Schreibt Mail in DB. True = neu gespeichert, False = bereits vorhanden."""
    message_id = record["message_id"]
    if not message_id or mail_exists(conn, message_id):
        return False

    subject          = record["subject"] or "(kein Betreff)"
    attachments_json = json.dumps(record["attachments"], ensure_ascii=False)

    with conn:
        cursor = conn.execute(
            """INSERT INTO documents
               (project_id, hash, filename, extension, filesize, modified_at,
                extraction_status, source_type, metadata)
               VALUES (?, ?, ?, '.eml', 0, ?, 'ok', 'email', ?)""",
            (project_id, message_id, subject,
             record["mail_date"], attachments_json),
        )
        doc_id = cursor.lastrowid

        conn.execute(
            """INSERT INTO mails (document_id, sender, recipients, cc, subject, date, thread_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (doc_id, record["sender"], record["recipients"], record["cc"],
             record["subject"], record["mail_date"], record["thread_id"]),
        )

        if record["cleaned_text"]:
            conn.execute(
                """INSERT INTO document_content (document_id, content, language)
                   VALUES (?, ?, 'de')""",
                (doc_id, record["cleaned_text"]),
            )

    return True


# ── IMAP-Fetch-Helpers ────────────────────────────────────────────────────────

def _fetch_uids(client: imaplib.IMAP4_SSL, mailbox: str) -> list:
    # READ-ONLY: kein STORE/DELETE — nur SELECT readonly + FETCH PEEK
    status, _ = client.select(f'"{mailbox}"', readonly=True)
    if status != "OK":
        raise RuntimeError(f"Postfach nicht öffenbar: {mailbox}")
    status, data = client.search(None, "ALL")
    if status != "OK":
        raise RuntimeError(f"Suche fehlgeschlagen: {mailbox}")
    return data[0].split() if data and data[0] else []


def _fetch_header(client: imaplib.IMAP4_SSL, uid):
    # BODY.PEEK liest ohne \Seen-Flag zu setzen
    status, data = client.fetch(uid, "(BODY.PEEK[HEADER])")
    if status != "OK" or not data or data[0] is None:
        raise RuntimeError(f"Header-Abruf fehlgeschlagen UID {uid!r}")
    return message_from_bytes(data[0][1])


def _fetch_full(client: imaplib.IMAP4_SSL, uid):
    status, data = client.fetch(uid, "(RFC822)")
    if status != "OK" or not data or data[0] is None:
        raise RuntimeError(f"Mail-Abruf fehlgeschlagen UID {uid!r}")
    return message_from_bytes(data[0][1])


# ── Postfach scannen ──────────────────────────────────────────────────────────

def scan_mailbox(client: imaplib.IMAP4_SSL, mailbox: str, project_id: int) -> dict:
    """Scannt ein Postfach incremental. Gibt {new, skipped, errors} zurück."""
    conn   = connection.get_connection()
    new    = skipped = errors = 0

    try:
        uids = _fetch_uids(client, mailbox)
        log.info("Postfach '%s': %d Nachrichten", mailbox, len(uids))

        for uid in uids:
            try:
                header_msg = _fetch_header(client, uid)
                message_id = (header_msg.get("Message-ID") or "").strip()

                if not message_id or mail_exists(conn, message_id):
                    skipped += 1
                    continue

                full_msg = _fetch_full(client, uid)
                record   = build_email_record(full_msg, mailbox)

                if save_mail_to_db(conn, record, project_id):
                    new += 1
                else:
                    skipped += 1

            except Exception as exc:
                errors += 1
                log.warning("UID %s in '%s' übersprungen: %s", uid, mailbox, exc)

        # Statistik aktualisieren
        with conn:
            conn.execute(
                """UPDATE mail_scan_config
                   SET last_scanned_at = strftime('%Y-%m-%dT%H:%M:%SZ','now'),
                       mail_count      = mail_count + ?
                   WHERE mailbox_name = ?""",
                (new, mailbox),
            )

    finally:
        conn.close()

    log.info("'%s' fertig | neu=%d | übersprungen=%d | fehler=%d",
             mailbox, new, skipped, errors)
    return {"new": new, "skipped": skipped, "errors": errors}
