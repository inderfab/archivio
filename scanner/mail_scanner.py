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
    # Socket-Timeout: hängende IMAP-Operationen nach 30s abbrechen
    client   = imaplib.IMAP4_SSL(host, port)
    client.socket().settimeout(30)
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


def _encode_imap_utf7(text: str) -> str:
    """UTF-8 Postfachname → IMAP modified UTF-7 (RFC 3501) für client.select()."""
    result = []
    buf: list[str] = []

    def flush():
        if buf:
            raw = "".join(buf).encode("utf-16-be")
            b64 = base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",")
            result.append(f"&{b64}-")
            buf.clear()

    for ch in text:
        if ch == "&":
            flush()
            result.append("&-")
        elif 0x20 <= ord(ch) <= 0x7E:
            flush()
            result.append(ch)
        else:
            buf.append(ch)
    flush()
    return "".join(result)


# ── Projekt-Matching ──────────────────────────────────────────────────────────

def match_mailbox_to_project(conn, mailbox_name: str) -> int | None:
    """Postfachname enthält Projektnummer → project_id oder None.

    Sucht die Nummer im Ordnernamen des Pfades (letztes Segment) sowie im
    Projektnamen — damit "211_Derendingen" zu "211 Emmenhof Derendingen" passt.
    """
    m = re.search(r'\d{3,}', mailbox_name)
    if not m:
        return None
    number = m.group(0)
    rows = conn.execute(
        """SELECT id, path, name FROM projects
           WHERE path LIKE ? OR name LIKE ?""",
        (f"%{number}%", f"%{number}%"),
    ).fetchall()
    if not rows:
        return None
    # Bevorzuge Treffer, wo die Nummer am Wortanfang steht (z.B. "211 Emmenhof"
    # schlägt einen zufälligen Treffer "12211_Archiv")
    for row in rows:
        folder = row["path"].rstrip("/").rsplit("/", 1)[-1]
        proj_number = re.match(r'(\d{3,})', row["name"] or "")
        if proj_number and proj_number.group(1) == number:
            return row["id"]
        if re.match(rf'{re.escape(number)}\D', folder) or folder.startswith(number):
            return row["id"]
    # Fallback: erster Treffer
    return rows[0]["id"]


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
    """Nur Dateinamen — kein get_payload(decode=True), kein Anhang-RAM."""
    attachments = []
    for part in message.walk():
        filename = part.get_filename()
        if not filename:
            continue
        fname = decode_mime_header(filename)
        ext   = Path(fname).suffix.lower()
        attachments.append({"name": fname, "extension": ext})
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

    sender  = f"{sender_name} <{sender_email}>".strip(" <>") if sender_email else sender_name
    cleaned = clean_mail_text(raw_text)
    if attachments:
        att_line = "Anhänge: " + ", ".join(a["name"] for a in attachments)
        cleaned  = (cleaned + "\n\n" + att_line) if cleaned else att_line

    return {
        "message_id":   message_id,
        "mail_date":    parse_mail_date(message.get("Date", "")),
        "sender":       sender,
        "sender_email": sender_email,
        "recipients":   to_emails,
        "cc":           cc_emails,
        "subject":      subject,
        "raw_text":     raw_text,
        "cleaned_text": cleaned,
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


def save_mail_to_db(conn, record: dict, project_id: int | None, mailbox_name: str = "",
                    project_name: str = "") -> bool:
    """Schreibt Mail in DB inkl. Chunks + Embeddings.
    Jede Stufe hat eigenes try/except — Fehler werden geloggt, Mail wird nicht übersprungen.
    True = neu gespeichert, False = bereits vorhanden.
    """
    import threading as _threading
    message_id = record["message_id"]
    if not message_id or mail_exists(conn, message_id):
        return False

    subject          = record["subject"] or "(kein Betreff)"
    attachments_json = json.dumps(record["attachments"], ensure_ascii=False)
    cleaned_text     = record["cleaned_text"] or ""

    # Stufe 1: Dokument + Mail-Metadaten + Content speichern
    try:
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
                """INSERT INTO mails (document_id, sender, recipients, cc, subject, date, thread_id, mailbox_name)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (doc_id, record["sender"], record["recipients"], record["cc"],
                 record["subject"], record["mail_date"], record["thread_id"], mailbox_name),
            )
            if cleaned_text:
                conn.execute(
                    "INSERT INTO document_content (document_id, content, language) VALUES (?, ?, 'de')",
                    (doc_id, cleaned_text),
                )
    except Exception as e:
        log.warning("Mail speichern fehlgeschlagen (%s): %s", subject[:60], e)
        return False

    # Stufe 1b: Volltext INKL. Signatur für Rubrica spiegeln (separate DB, eigene Fehlerkapsel —
    # darf den Mail-Scan nie beeinträchtigen). No-op wenn rubrica.enabled nicht gesetzt ist.
    try:
        from db.rubrica import save_signature_source
        save_signature_source(record, project_name, mailbox_name)
    except Exception as e:
        log.warning("Rubrica-Spiegelung fehlgeschlagen (%s): %s", subject[:60], e)

    if not cleaned_text:
        return True

    # Stufe 2: Chunks erstellen
    try:
        from scanner.extractors import split_text_into_chunks
        from db import queries
        parts = split_text_into_chunks(cleaned_text)
        if parts:
            chunks = [{"page_number": None, "chunk_index": i, "content": p}
                      for i, p in enumerate(parts)]
            with conn:
                queries.save_chunks(conn, doc_id, chunks)
    except Exception as e:
        log.warning("Chunking fehlgeschlagen doc %d (%s): %s", doc_id, subject[:60], e)
        return True  # Mail gespeichert, nur ohne Chunks

    # Stufe 3: Embeddings — eigene Connection, 30s Timeout
    try:
        from scanner.embedder import embed_document_chunks, is_ollama_running
        if is_ollama_running():
            done_flag = []
            def _do_embed():
                try:
                    ec = connection.get_connection()
                    embed_document_chunks(ec, doc_id)
                    ec.close()
                    done_flag.append(True)
                except Exception as ee:
                    log.warning("Embedding fehlgeschlagen doc %d: %s", doc_id, ee)
            t = _threading.Thread(target=_do_embed, daemon=True)
            t.start()
            t.join(timeout=30)
            if not done_flag:
                log.warning("Embedding Timeout doc %d (%s) — übersprungen", doc_id, subject[:60])
    except Exception as e:
        log.warning("Embedding-Start fehlgeschlagen doc %d: %s", doc_id, e)

    return True


# ── IMAP-Fetch-Helpers ────────────────────────────────────────────────────────

def _fetch_uids(client: imaplib.IMAP4_SSL, mailbox: str) -> list:
    # READ-ONLY: kein STORE/DELETE — nur SELECT readonly + FETCH PEEK
    # Postfachname muss in IMAP modified UTF-7 kodiert werden (RFC 3501)
    imap_name = _encode_imap_utf7(mailbox)
    status, _ = client.select(f'"{imap_name}"', readonly=True)
    if status != "OK":
        raise RuntimeError(f"Postfach nicht öffenbar: {mailbox!r}")
    status, data = client.search(None, "ALL")
    if status != "OK":
        raise RuntimeError(f"Suche fehlgeschlagen: {mailbox!r}")
    return data[0].split() if data and data[0] else []


def _fetch_header(client: imaplib.IMAP4_SSL, uid):
    # BODY.PEEK liest ohne \Seen-Flag zu setzen
    status, data = client.fetch(uid, "(BODY.PEEK[HEADER])")
    if status != "OK" or not data or data[0] is None:
        raise RuntimeError(f"Header-Abruf fehlgeschlagen UID {uid!r}")
    return message_from_bytes(data[0][1])


_MAX_MAIL_FETCH = 524288  # 512 KB — Header + Text, keine Anhang-Daten via IMAP


def _fetch_full(client: imaplib.IMAP4_SSL, uid):
    """Partial-Fetch: nur erste 512 KB — reicht für Header + Text, keine Anhänge im RAM."""
    status, data = client.fetch(uid, f"(BODY.PEEK[]<0.{_MAX_MAIL_FETCH}>)")
    if status != "OK" or not data or data[0] is None:
        raise RuntimeError(f"Mail-Abruf fehlgeschlagen UID {uid!r}")
    return message_from_bytes(data[0][1])


# ── Postfach scannen ──────────────────────────────────────────────────────────

def scan_mailbox(client: imaplib.IMAP4_SSL, mailbox: str, project_id: int,
                 progress: dict | None = None) -> dict:
    """Scannt ein Postfach incremental. Gibt {new, skipped, errors} zurück.
    progress: optionales Dict, das mit processed/total aktualisiert wird (für Nav-Status).
    """
    conn   = connection.get_connection()
    new    = skipped = errors = 0

    # Einmal pro Postfach (nicht pro Mail) auflösen — für die Rubrica-Spiegelung, die den
    # Projektnamen denormalisiert mitspeichert (kein Cross-DB-Join für Rubrica nötig).
    project_row  = conn.execute("SELECT name FROM projects WHERE id=?", (project_id,)).fetchone()
    project_name = project_row["name"] if project_row else ""

    try:
        uids = _fetch_uids(client, mailbox)
        log.info("Postfach '%s': %d Nachrichten", mailbox, len(uids))
        if progress is not None:
            progress["total"]     = len(uids)
            progress["processed"] = 0

        for uid in uids:
            try:
                header_msg = _fetch_header(client, uid)
                message_id = (header_msg.get("Message-ID") or "").strip()

                if not message_id or mail_exists(conn, message_id):
                    skipped += 1
                    if progress is not None:
                        progress["processed"] = progress.get("processed", 0) + 1
                    continue

                full_msg = _fetch_full(client, uid)
                record   = build_email_record(full_msg, mailbox)

                # save_mail_to_db inkl. Chunking + Embedding in eigenem Thread (60s Timeout)
                result_box: list = []
                def _save(rec=record, mb=mailbox):
                    try:
                        result_box.append(save_mail_to_db(conn, rec, project_id, mb, project_name))
                    except Exception as exc:
                        log.warning("save_mail_to_db Fehler UID %s: %s", uid, exc)
                        result_box.append(False)
                import threading as _t
                t = _t.Thread(target=_save, daemon=True)
                t.start()
                t.join(timeout=60)
                if not result_box:
                    log.warning("UID %s Timeout (60s) — übersprungen", uid)
                    errors += 1
                elif result_box[0]:
                    new += 1
                else:
                    skipped += 1
                if progress is not None:
                    progress["processed"] = progress.get("processed", 0) + 1

            except Exception as exc:
                errors += 1
                if progress is not None:
                    progress["processed"] = progress.get("processed", 0) + 1
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
