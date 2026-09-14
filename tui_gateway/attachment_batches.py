"""Request-bound owner uploads, indexed beside native published artifacts.

A durable admission receipt prevents an ACK loss from replaying a turn. A
process failure during dispatch is explicitly uncertain, never auto-replayed.
The canonical artifact store owns all bytes; this is not another auth/file store.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import stat
import threading
import time
import uuid
from pathlib import Path

from gateway import published_artifacts as artifacts

_LOCK = threading.RLock()
_PROCESS = secrets.token_hex(16)
MAX_ITEMS = 8
MAX_BYTES = 15 * 1024 * 1024
MAX_BATCH_BYTES = 60 * 1024 * 1024


class BatchError(ValueError):
    pass


GENERIC_REFUSAL = "This attachment message was not admitted. You can change its files and send again."
SEALED_REFUSAL = ("This attachment message was already refused, and its identity cannot be "
                  "reused. Send these files as a new message.")


class ItemRefused(BatchError):
    """One named item cannot be admitted, and the Owner is told which one.

    A submission is still admitted whole -- Hermes never delivers half a turn --
    but "one of your files is the problem, and here is which and why" is the
    Owner's only route back to a send that works. The reason is composed here,
    from server-side facts, so every client renders the same sentence instead of
    inventing its own from an error code.
    """
    def __init__(self, reason, *, item_id=None, filename=None, code="unsupported_item"):
        super().__init__(reason)
        self.refusal = {"reason": reason, "code": code,
                        "item_id": item_id, "filename": filename}


def refusal_of(exc):
    """The client-safe refusal detail a typed batch error carries, if any."""
    refusal = getattr(exc, "refusal", None)
    return refusal if isinstance(refusal, dict) and refusal.get("reason") else None


class RefusedBatch(BatchError):
    """A durable terminal identity, not an inference from an error message."""
    def __init__(self, batch_id, message_id, refusal=None):
        refusal = refusal if isinstance(refusal, dict) and refusal.get("reason") else None
        super().__init__((refusal or {}).get("reason") or GENERIC_REFUSAL)
        record = {"schema_version": 1, "batch_id": batch_id, "client_message_id": message_id,
                  "admission": "refused", "retry_with_new_identity": True}
        if refusal:
            # Persisted with the tombstone, so a resend of the same rejected
            # identity is answered with the same specific sentence rather than
            # decaying into the generic refusal on every attempt after the first.
            record["refusal"] = refusal
        self.refusal = refusal
        self.data = {"attachment_batch": record}


def _stored_refusal(row):
    """The refusal detail a terminal row kept, or ``None`` for a legacy tombstone.

    ``receipt`` carries one of two disjoint shapes and is never both: the
    acceptance receipt written after a successful dispatch, or -- only on a
    ``refused`` row, which every reader rejects before it reaches the acceptance
    path -- this refusal detail. It was already in use for the former.
    """
    try:
        stored = json.loads(row["receipt"]) if row["receipt"] else None
    except (TypeError, ValueError, IndexError):
        return None
    detail = stored.get("refusal") if isinstance(stored, dict) else None
    return detail if isinstance(detail, dict) and detail.get("reason") else None


def _replay_refusal(stored, decoded):
    """The refusal a sealed identity may repeat for the submission now in hand.

    Repeating the first reason is honest only while the item it names is still
    one of the files being sent. "You can change its files and send again" is
    exactly what the Owner is invited to do, and once they have, naming the old
    file describes a submission that no longer exists. The reply then becomes the
    one thing still true: this identity is spent, so send a new message.

    ``decoded`` is the caller's current items; a caller that cannot see them
    passes an empty sequence and never repeats an item-naming reason.
    """
    if not stored:
        return None
    if stored.get("item_id") is None and stored.get("filename") is None:
        return stored  # Names no item, so no submission can contradict it.
    if any(item_id == stored.get("item_id") and name == stored.get("filename")
           for item_id, name, *_ in decoded):
        return stored
    return {"reason": SEALED_REFUSAL, "code": "identity_refused",
            "item_id": None, "filename": None}


def _cleanup_execution_batch(home, batch_id):
    """Remove only flat execution copies owned by one unadmitted batch.

    Walk with ``lstat`` so a redirected profile/attachments component is never
    followed during cleanup. Canonical published blobs live under a separate
    root and are deliberately outside this helper.
    """
    home = Path(os.path.abspath(home))
    target = home / "attachments" / ("batch-" + batch_id)
    relative = Path(os.path.abspath(target)).relative_to(home)
    current = home.parent.resolve()
    for component in (home.name, *relative.parts):
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(info.st_mode):
            return
    try:
        children = list(current.iterdir())
    except OSError:
        return
    for child in children:
        try:
            info = child.lstat()
            if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                child.unlink()
        except (FileNotFoundError, OSError):
            pass
    try:
        current.rmdir()
    except OSError:
        pass


def seal_refusal(home, session_id, batch_id, message_id, refusal=None):
    """Reserve both identities atomically against stage/claim, or return no proof.

    The caller must already have resolved the authenticated native session. Room
    callers must exclude a pre-upgrade canonical event before using this helper.
    No state after the dispatch boundary is eligible, even a failed response.

    ``refusal`` is the client-safe detail behind this refusal, composed from the
    items the caller is holding right now. It is stored with the tombstone so a
    bare resend of the same identity still gets a reason instead of decaying into
    the generic sentence. A caller that supplies its own reason is describing the
    current submission and that reason wins; a caller that supplies none cannot
    confirm the stored reason still names a file being sent, so an item-naming
    tombstone answers with ``SEALED_REFUSAL`` rather than a stale filename.
    """
    batch_id, message_id = identity(batch_id), identity(message_id)
    if not session_id:
        return None
    refusal = refusal if isinstance(refusal, dict) and refusal.get("reason") else None
    receipt = json.dumps({"refusal": refusal}) if refusal else None
    with _LOCK:
        conn = _connect(home)
        cleanup = []
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = _row(conn, batch_id, message_id, session_id)
            if row is None:
                if conn.execute("SELECT 1 FROM attachment_batches WHERE client_message_id=?", (message_id,)).fetchone():
                    return None
                conn.execute("""INSERT INTO attachment_batches
                    (batch_id,client_message_id,session_id,fingerprint,records,state,receipt)
                    VALUES (?,?,?,'','[]','refused',?)""", (batch_id, message_id, session_id, receipt))
            elif row["state"] == "refused":
                # The seal keeps the reason it was first sealed with; only a
                # legacy tombstone sealed before reasons were carried is filled
                # in. What is *returned* has to describe the submission in hand,
                # so a caller with no reason of its own never repeats a filename
                # it cannot see in what was just sent.
                stored = _stored_refusal(row)
                if refusal is None:
                    refusal = _replay_refusal(stored, ())
                elif stored is None and receipt is not None:
                    conn.execute("UPDATE attachment_batches SET receipt=? WHERE batch_id=?",
                                 (receipt, batch_id))
            elif row["state"] == "staged" and row["process"] is None and row["submit_fingerprint"] is None:
                # Validation failed before admission. Retain only the identity
                # tombstone; the unaccepted bytes and artifact rows have no
                # durable message that could ever own them.
                records = json.loads(row["records"])
                artifact_ids = [item.get("artifact_id") for item in records if
                                isinstance(item, dict) and isinstance(item.get("artifact_id"), str)]
                for artifact_id in artifact_ids:
                    cleanup.append(artifacts._blob_path(home, artifact_id))
                    conn.execute("DELETE FROM published_artifacts WHERE artifact_id=?", (artifact_id,))
                conn.execute(
                    "UPDATE attachment_batches SET state='refused',records='[]',receipt=? WHERE batch_id=?",
                    (receipt, batch_id))
            else:
                return None
            conn.commit()
            for path in cleanup:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass  # unindexed private orphan; normal artifact cleanup may reap it
            # This namespace is unique to the refused UUID. Clean it even for
            # an existing tombstone so a retry can repair a crash that occurred
            # after one execution copy but before the dispatch claim commit.
            _cleanup_execution_batch(home, batch_id)
            return RefusedBatch(batch_id, message_id, refusal)
        except BatchError:
            # A different session/identity is neither a disclosure nor a proof.
            return None
        finally:
            conn.close()


def identity(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise BatchError("batch_id, client_message_id and item_id must be UUIDs") from None


def _private_directory(path, home):
    """Refuse redirected components before creating anything below them."""
    home = Path(os.path.abspath(home))
    relative = Path(os.path.abspath(path)).relative_to(home)
    current = home.parent.resolve()
    for component in (home.name, *relative.parts):
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            info = current.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise BatchError("Attachment storage is not a private local directory")
    return current


def _store(home):
    directory = _private_directory(Path(home) / "artifacts" / "published", home)
    # SQLite opens these itself. Reject redirected databases AND journal files
    # before handing the canonical native index to SQLite.
    for name in ("index.db", "index.db-wal", "index.db-shm", "index.db-journal"):
        path = directory / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise BatchError("Attachment index is not a private regular file")
    return directory


def _read_blob(path):
    # O_NOFOLLOW covers a replaced leaf, unlike is_symlink()+read_bytes().
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
            raise BatchError("Attachment is not a bounded private regular file")
        return stream.read(MAX_BYTES + 1)


def _connect(home):
    _store(home)
    conn = artifacts._connect(home)
    conn.execute("""CREATE TABLE IF NOT EXISTS attachment_batches (
        batch_id TEXT PRIMARY KEY, client_message_id TEXT NOT NULL UNIQUE,
        session_id TEXT NOT NULL, fingerprint TEXT NOT NULL, records TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'staged', submit_fingerprint TEXT,
        receipt TEXT, process TEXT)""")
    conn.commit()
    return conn


def _decode(items):
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise BatchError("A batch needs between 1 and 8 attachments")
    decoded, seen, total = [], set(), 0
    for item in items:
        if not isinstance(item, dict):
            raise BatchError("Invalid attachment")
        item_id = identity(item.get("item_id"))
        if item_id in seen:
            raise BatchError("Duplicate item_id")
        seen.add(item_id)
        name, mime, kind = item.get("filename"), item.get("mime_type"), item.get("kind")
        if not isinstance(name, str) or not name.strip() or kind not in {"image", "file"}:
            raise BatchError("Attachment needs a filename and image/file kind")
        name = re.sub(r"[\x00-\x1f\x7f]", "_", name.strip().replace("\\", "/").split("/")[-1])[:255]
        if not name or name in {".", ".."}:
            raise BatchError("Invalid attachment filename")
        if not isinstance(mime, str) or not re.fullmatch(r"[\w.+-]+/[\w.+-]+", mime):
            raise ItemRefused(f'"{name}" did not arrive with a usable file type.',
                              item_id=item_id, filename=name, code="mime_invalid")
        if mime.startswith("image/"):
            kind = "image"  # An image chosen through the document picker is still an image.
        raw = item.get("data_base64")
        limit_mb = MAX_BYTES // (1024 * 1024)
        if not isinstance(raw, str) or len(raw) > (MAX_BYTES + 2) // 3 * 4:
            raise ItemRefused(f'"{name}" is larger than the {limit_mb} MB limit for one attachment.',
                              item_id=item_id, filename=name, code="item_too_large")
        try:
            data = base64.b64decode(raw, validate=True)
        except Exception:
            raise ItemRefused(f'"{name}" did not arrive intact. Attach it again.',
                              item_id=item_id, filename=name, code="data_corrupt") from None
        total += len(data)
        if not data:
            raise ItemRefused(f'"{name}" is empty.',
                              item_id=item_id, filename=name, code="item_empty")
        if len(data) > MAX_BYTES:
            raise ItemRefused(f'"{name}" is larger than the {limit_mb} MB limit for one attachment.',
                              item_id=item_id, filename=name, code="item_too_large")
        if total > MAX_BATCH_BYTES:
            raise BatchError(
                f"These attachments come to more than the {MAX_BATCH_BYTES // (1024 * 1024)} MB "
                "limit for one message.")
        if kind == "image":
            mime = _admit_image(item_id, name, mime, data)
        decoded.append((item_id, name, mime, kind, data))
    return decoded


# Pillow names the *reader* that opened the bytes, which is not always the wire
# type of the file. A camera still carrying an MPF multi-picture segment -- what
# every recent iPhone writes for an HDR gain map -- opens through the
# multi-picture reader while staying an ordinary JPEG to the filesystem, to the
# picker that chose it, and to the model provider that will read it. The HEIF
# reader (pillow-heif, when installed) opens the whole HEIC/HEIF family and
# registers it as image/heif, while the signature table the vision path trusts
# names that family image/heic. Only readers whose registered MIME is not the
# wire type belong here; a reader Pillow already names correctly (DIB is
# registered image/bmp) needs no entry.
_IMAGE_READER_MIME = {"MPO": "image/jpeg", "HEIF": "image/heic"}

# Wire synonyms clients legitimately send for the same bytes.
_IMAGE_MIME_SYNONYMS = {
    "image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg", "image/x-jpeg": "image/jpeg",
    "image/mpo": "image/jpeg", "image/x-png": "image/png", "image/x-citrix-png": "image/png",
    "image/x-ms-bmp": "image/bmp", "image/x-bmp": "image/bmp", "image/tif": "image/tiff",
}


def _canonical_image_mime(value):
    value = (value or "").strip().lower()
    return _IMAGE_MIME_SYNONYMS.get(value, value)


def supported_image_mime_types():
    """Image wire types this build can actually read, for client pre-flight.

    Derived from the installed decoders, not a hand-kept list. A client that
    knows the server has no HEIF decoder can ask iOS for a JPEG at the picker
    instead of uploading a still the vision path will skip. Admission itself
    is broader than this list: any bytes a reader opens, or a signature
    recognises, are accepted with the stored type read off the bytes.
    """
    try:
        from PIL import Image
        Image.init()
        values = set()
        for reader in Image.OPEN:
            mime = _canonical_image_mime(_IMAGE_READER_MIME.get(reader) or Image.MIME.get(reader))
            if mime.startswith("image/"):
                values.add(mime)
        return sorted(values)
    except Exception:
        # Empty means "this build could not answer", and the caller omits the
        # field rather than telling a client that no image is ever admissible.
        return []


def _image_reader(data):
    """The Pillow reader that fully decodes these bytes, or ``None``."""
    from io import BytesIO
    from PIL import Image
    try:
        with Image.open(BytesIO(data)) as image:
            reader = image.format
            image.verify()
    except Exception:
        return None
    return reader or None


def _reader_wire_mime(reader):
    """The wire type this build registers for a reader, or ``None`` if it has none.

    ``None`` is not "anything goes". A reader this build cannot name a wire type
    for corroborates no declared type either, and in Pillow 12.3 that is 23 of
    the 43 installed readers -- DDS, QOI, WMF, MSP, IM, SPIDER and the rest. The
    caller stores a type synthesised from the reader's own name instead -- never
    the declared label -- so the durable record and the file route stay truthful.
    """
    from PIL import Image
    if reader in _IMAGE_READER_MIME:
        return _IMAGE_READER_MIME[reader]
    Image.init()  # register every plugin before consulting the MIME table
    return _canonical_image_mime(Image.MIME.get(reader)) or None


def _sniff_image_mime(data):
    """The wire type this build's image signatures recognise in the bytes, or ``None``.

    The signature table is ``agent.image_routing``'s -- one table for admission
    and the vision path -- so a format recognised here is one the rest of the
    pipeline also knows. SVG stays a document: it is vector markup the vision
    path skips by design, and text served back as an image is the type
    confusion these checks exist to prevent.
    """
    from agent.image_routing import _sniff_mime_from_bytes
    mime = _sniff_mime_from_bytes(data)
    return mime if mime and mime != "image/svg+xml" else None


def _admit_image(item_id, name, mime, data):
    """Admit any image these bytes support; return the wire type to store.

    The stored type is read off the bytes, never trusted from the declared
    label: iOS answers a requested JPEG with PNG bytes for some screenshots,
    and the send must not be lost to a mismatch -- nor may the durable record
    wear a type its bytes contradict. A signature match admits formats no
    reader opens in this build, like iPhone HEIC originals, while a file whose
    type this build can indeed decode is still answered as damaged when the
    reader cannot open it. Only bytes no reader opens and no signature claims
    are refused.
    """
    reader = _image_reader(data)
    if reader is not None:
        return _reader_wire_mime(reader) or f"image/{reader.lower()}"
    guessed = _sniff_image_mime(data)
    if guessed is not None and guessed not in supported_image_mime_types():
        return guessed
    raise ItemRefused(
        f'"{name}" could not be read as an image. What arrived is damaged, '
        "incomplete, or not really a picture. Re-export it and send it again.",
        item_id=item_id, filename=name, code="image_unreadable")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _row(conn, batch_id, message_id, session_id):
    row = conn.execute("SELECT * FROM attachment_batches WHERE batch_id=?", (batch_id,)).fetchone()
    if row and (row["client_message_id"] != message_id or row["session_id"] != session_id):
        raise BatchError("Batch does not belong to this message and session")
    return row


def stage(home, profile, session_id, batch_id, message_id, items):
    batch_id, message_id = identity(batch_id), identity(message_id)
    if not session_id:
        raise BatchError("A canonical session is required")
    decoded = _decode(items)  # Entire batch validated before any filesystem/DB effect.
    fingerprint = _digest([(i, n, m, k, hashlib.sha256(d).hexdigest()) for i, n, m, k, d in decoded])
    with _LOCK:
        conn = _connect(home)
        created = []
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = _row(conn, batch_id, message_id, session_id)
            if row:
                if row["state"] == "refused":
                    # This caller holds the items, so the sealed reason is
                    # repeated only while it still names one of them.
                    raise RefusedBatch(batch_id, message_id,
                                       _replay_refusal(_stored_refusal(row), decoded))
                if row["fingerprint"] != fingerprint:
                    raise BatchError("Batch identity already names different attachments")
                return {"batch_id": batch_id, "client_message_id": message_id,
                        "attachments": json.loads(row["records"]), "state": row["state"]}
            if conn.execute("SELECT 1 FROM attachment_batches WHERE client_message_id=?", (message_id,)).fetchone():
                raise BatchError("Message identity already has an attachment batch")
            records = []
            for item_id, name, mime, kind, data in decoded:
                artifact_id = secrets.token_hex(16)
                path = artifacts._blob_path(home, artifact_id)
                # Exclusive creation, never resolve an Owner-supplied path.
                _store(home)
                with path.open("xb") as stream:
                    created.append(path)
                    stream.write(data)
                record = artifacts.PublishedArtifact(artifact_id, name, mime, len(data),
                    hashlib.sha256(data).hexdigest(), str(profile), session_id, time.time())
                conn.execute("""INSERT INTO published_artifacts VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (artifact_id, name, mime, len(data), record.sha256, str(profile), session_id,
                     str(path), record.created_at, "available"))
                records.append({**record.to_dict(), "item_id": item_id, "kind": kind})
            conn.execute("""INSERT INTO attachment_batches
                (batch_id,client_message_id,session_id,fingerprint,records) VALUES (?,?,?,?,?)""",
                (batch_id, message_id, session_id, fingerprint, json.dumps(records)))
            conn.commit()
            return {"batch_id": batch_id, "client_message_id": message_id,
                    "attachments": records, "state": "staged"}
        except BaseException:
            conn.rollback()
            for path in created:
                path.unlink(missing_ok=True)
            raise
        finally:
            conn.close()


def submit(home, session_id, batch_id, message_id, payload, dispatch):
    """Claim once, then invoke native admission. Repeated receipts never dispatch.

    Dispatch is synchronous admission only, not a model call. SQLite serializes
    competing gateway processes; no DB lock is held while admitting the turn.
    """
    batch_id, message_id = identity(batch_id), identity(message_id)
    fingerprint = _digest(payload)
    with _LOCK:
        conn = _connect(home)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = _row(conn, batch_id, message_id, session_id)
            if row is None:
                raise BatchError("Unknown attachment batch; stage it first")
            if row["state"] == "refused":
                # A submit carries no items of its own and a refused row keeps
                # none, so nothing here can confirm a sealed filename is still
                # part of what is being sent.
                raise RefusedBatch(batch_id, message_id,
                                   _replay_refusal(_stored_refusal(row), ()))
            # The previous implementation reset a generic dispatch error to
            # staged but retained its process marker. That is NOT a never-
            # admitted upload and must remain uncertain across this upgrade.
            if row["state"] == "staged" and row["process"] is not None:
                return {"status": "outcome_unknown", "delivery_state": "dispatching", "replayed": True,
                        "attachments": json.loads(row["records"]), "batch_id": batch_id,
                        "client_message_id": message_id}
            if row["submit_fingerprint"] and row["submit_fingerprint"] != fingerprint:
                raise BatchError("Message identity already names a different submission")
            records = json.loads(row["records"])
            if row["state"] != "staged":
                receipt = json.loads(row["receipt"]) if row["receipt"] else {"status": "outcome_unknown"}
                if row["process"] != _PROCESS and row["state"] in {"accepted", "dispatching"}:
                    receipt = {**receipt, "status": "outcome_unknown"}
                return {**receipt, "replayed": True, "delivery_state": row["state"], "attachments": records,
                        "batch_id": batch_id, "client_message_id": message_id}
            # Verify every native artifact before claiming, not partway through dispatch.
            paths = []
            for record in records:
                path = artifacts._blob_path(home, record["artifact_id"])
                available = conn.execute("SELECT 1 FROM published_artifacts WHERE artifact_id=? AND status='available'",
                                         (record["artifact_id"],)).fetchone()
                if not available or hashlib.sha256(_read_blob(path)).hexdigest() != record["sha256"]:
                    raise BatchError("An attachment is unavailable; this batch was not submitted")
                paths.append(str(path))
            # Native context preprocessing grants only the receiving profile's
            # attachment directory when its project cwd lives elsewhere.
            paths = _execution_copies(home, "batch-" + batch_id, [
                (artifacts.PublishedArtifact(**{key: record[key] for key in
                    ("artifact_id", "filename", "mime_type", "size_bytes", "sha256", "profile", "session_id", "created_at")}), Path(path))
                for record, path in zip(records, paths)])
            conn.execute("UPDATE attachment_batches SET state='dispatching',submit_fingerprint=?,process=? WHERE batch_id=?",
                         (fingerprint, _PROCESS, batch_id))
            conn.commit()
            response = dispatch(records, paths)
            if response.get("error"):
                # A generic error is not a transactional proof of no effects.
                # Keep the dispatched identity uncertain and never unlock it.
                return response
            receipt = {**response.get("result", {}), "delivery_state": "accepted", "batch_id": batch_id,
                       "client_message_id": message_id, "attachments": records}
            conn.execute("UPDATE attachment_batches SET state=CASE WHEN state='dispatching' THEN 'accepted' ELSE state END,receipt=? WHERE batch_id=?",
                         (json.dumps(receipt), batch_id))
            conn.commit()
            return receipt
        finally:
            conn.close()


def complete(home, batch_id, status):
    with _LOCK:
        conn = _connect(home)
        try:
            conn.execute("UPDATE attachment_batches SET state=? WHERE batch_id=? AND state IN ('dispatching','accepted')",
                         ("completed" if status == "complete" else "failed", identity(batch_id)))
            conn.commit()
        finally:
            conn.close()


def room_records(home, room_id, references):
    """Resolve only native blobs uploaded for this exact room, never arbitrary IDs."""
    from gateway.hosted_room_discussion import validate_attachments
    _store(home)
    result = []
    for reference in validate_attachments(references):
        resolved = artifacts.resolve_blob(home, reference["artifact_id"])
        if resolved is None:
            raise BatchError("A room attachment is unavailable")
        path, record = resolved
        if (record.session_id != f"room:{room_id}" or path.is_symlink()
                or any(record.to_dict()[key] != reference[key] for key in reference)):
            raise BatchError("Attachment does not belong to this room")
        if hashlib.sha256(_read_blob(path)).hexdigest() != record.sha256:
            raise BatchError("A room attachment is unavailable")
        result.append((record, path))
    return result


def _execution_copies(target_home, namespace, records):
    directory = _private_directory(Path(target_home) / "attachments" / namespace, target_home)
    paths, created = [], []
    try:
        for record, source in records:
            suffix = Path(record.filename).suffix.lower()
            if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
                suffix = ".bin"
            target = directory / (record.artifact_id + suffix)
            if target.is_symlink():
                raise BatchError("Room attachment execution copy is not a regular file")
            data = _read_blob(source)
            if hashlib.sha256(data).hexdigest() != record.sha256:
                raise BatchError("A room attachment changed during preparation")
            try:
                with target.open("xb") as stream:
                    try:
                        stream.write(data)
                    except OSError:
                        target.unlink(missing_ok=True)
                        raise
                created.append(target)
            except FileExistsError:
                if hashlib.sha256(_read_blob(target)).hexdigest() != record.sha256:
                    raise BatchError("Room attachment execution copy changed; refusing to overwrite") from None
            paths.append(str(target))
        return paths
    except (BatchError, OSError) as exc:
        for path in reversed(created):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            directory.rmdir()
        except OSError:
            pass
        if isinstance(exc, BatchError):
            raise
        raise BatchError("Attachment execution copies could not be prepared") from None


def room_turn_context(home, target_home, room_id, references, prompt):
    """Execution copies use native attachment roots; canonical IDs stay shared."""
    records = room_records(home, room_id, references)
    paths = _execution_copies(target_home, "room-" + hashlib.sha256(room_id.encode()).hexdigest()[:24], records)
    lines, images = [prompt, "", "Attached inputs (in supplied order):"], []
    from agent.context_references import format_reference_value
    for index, ((record, _), target) in enumerate(zip(records, paths), 1):
        kind = "image" if record.mime_type.startswith("image/") else "file"
        lines.append(f"{index}. {record.filename} @{kind}:{format_reference_value(str(target))}")
        if kind == "image":
            images.append(str(target))
    return "\n".join(lines), {"caption": prompt, "image_paths": images,
                                "metadata": {"attachments": list(references)}}
