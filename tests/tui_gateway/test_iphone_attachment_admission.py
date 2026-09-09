"""Owner journeys from a physical iPhone, through real admission.

The corpus that shipped the admission rule used synthetic images only, so the
one case a phone actually produces -- a camera still whose JPEG carries an MPF
multi-picture segment -- was never sent through it. These tests send the files a
picker really hands over, and hold the one-message invariant while doing it.
"""
import base64
from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image
from tui_gateway import attachment_batches as batches

from tests.tui_gateway.test_attachment_batches import item, rpc  # noqa: F401

ARABIC = "راجع هذي الصورة وقل لي وش تشوف"


def iphone_camera_still():
    """A .JPG exactly as an iPhone writes it: a JPEG carrying its HDR gain map.

    Pillow opens this through the multi-picture reader. It is still an ordinary
    JPEG to the picker that chose it, to the filesystem, and to the provider.
    """
    stream = BytesIO()
    Image.new("RGB", (240, 180), (190, 60, 40)).save(
        stream, "MPO", quality=88,
        append_images=[Image.new("RGB", (240, 180), (40, 60, 190))])
    return stream.getvalue()


def plain_png():
    stream = BytesIO()
    Image.new("RGB", (8, 8), "red").save(stream, "PNG")
    return stream.getvalue()


def plain_jpeg():
    stream = BytesIO()
    Image.new("RGB", (64, 64), (9, 120, 200)).save(stream, "JPEG", quality=90)
    return stream.getvalue()


def heic_still():
    """An unconvertible iPhone original: no HEIF decoder is installed."""
    return b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00heicmif1" + b"\x00" * 512


def photo(name="IMG_4821.JPG", mime="image/jpeg"):
    return item(iphone_camera_still(), name, "image", mime)


def pdf():
    return item(b"%PDF-1.7\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF",
                "quote.pdf", "file", "application/pdf")


def docx():
    return item(b"PK\x03\x04" + b"\x00" * 96, "scope.docx", "file",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document")


def submit(server, mid, entries, text=ARABIC, **extra):
    return server._methods["prompt.submit"]("rid", dict(
        session_id="ui", text=text, client_message_id=mid,
        attachment_batch={"batch_id": mid, "items": entries}, **extra))


def one_owner_turn(session, mid, entries, caption=ARABIC, images=1):
    """The invariant every journey below shares: exactly one durable Owner turn."""
    envelope = session["queued_prompt"]
    assert not session.get("queued_prompts"), "a single send must not queue twice"
    assert envelope["display_metadata"]["client_message_ids"] == [mid]
    assert envelope["persist_text"] == caption, "the Owner's own words are the durable row"
    assert len(envelope["image_paths"]) == images
    assert [row["filename"] for row in envelope["display_metadata"]["attachments"]] == \
        [entry["filename"] for entry in entries]
    return envelope


# ── the reported defect ─────────────────────────────────────────────────────

def test_iphone_camera_still_with_arabic_caption_is_admitted(rpc):
    """The Owner's report: one photo, one caption, refused with 4004."""
    server, session = rpc
    mid = str(uuid4()); entries = [photo()]
    result = submit(server, mid, entries)
    assert "error" not in result, result.get("error")
    envelope = one_owner_turn(session, mid, entries)
    assert envelope["text"].startswith(ARABIC)
    assert "1. IMG_4821.JPG" in envelope["text"]


def test_camera_still_keeps_the_type_its_picker_declared(tmp_path):
    """Admission must not silently relabel the bytes it accepted."""
    mid = str(uuid4())
    staged = batches.stage(tmp_path, "alpha", "chat", mid, mid, [photo()])
    record = staged["attachments"][0]
    assert record["mime_type"] == "image/jpeg" and record["kind"] == "image"


@pytest.mark.parametrize("declared", ["image/jpeg", "image/jpg", "image/mpo"])
def test_every_honest_name_for_one_camera_still_is_admitted(tmp_path, declared):
    mid = str(uuid4())
    assert batches.stage(tmp_path, "alpha", "chat", mid, mid,
                         [photo(mime=declared)])["state"] == "staged"


# ── the rest of the picker's output ─────────────────────────────────────────

def test_pdf_with_text_is_one_owner_turn(rpc):
    server, session = rpc
    mid = str(uuid4()); entries = [pdf()]
    assert "error" not in submit(server, mid, entries)
    one_owner_turn(session, mid, entries, images=0)


def test_docx_with_text_is_one_owner_turn(rpc):
    server, session = rpc
    mid = str(uuid4()); entries = [docx()]
    assert "error" not in submit(server, mid, entries)
    one_owner_turn(session, mid, entries, images=0)


def test_photo_and_pdf_and_text_is_one_ordered_owner_turn(rpc):
    server, session = rpc
    mid = str(uuid4()); entries = [photo(), pdf()]
    assert "error" not in submit(server, mid, entries)
    envelope = one_owner_turn(session, mid, entries)
    assert envelope["text"].index("1. IMG_4821.JPG") < envelope["text"].index("2. quote.pdf")


def test_reply_target_and_caption_reach_the_turn_and_bind_the_identity(rpc, monkeypatch):
    """Reply context is not dropped, and it cannot be swapped under one identity."""
    server, session = rpc
    seen = []
    original = server._submit_prompt
    monkeypatch.setattr(server, "_submit_prompt",
                        lambda rid, params, ctx=None: seen.append((params, ctx)) or
                        original(rid, params, ctx))
    mid = str(uuid4()); entries = [photo()]
    assert "error" not in submit(server, mid, entries, reply_to={"message_id": 41})
    params, context = seen[-1]
    assert params["reply_to"] == {"message_id": 41}
    assert context["caption"] == ARABIC
    assert params["client_message_id"] == mid
    one_owner_turn(session, mid, entries)

    # The same identity with a different reply target is a different message.
    with pytest.raises(batches.BatchError, match="different submission"):
        batches.submit(session["profile_home"], "chat", mid, mid,
                       {"text": ARABIC, "reply_to": {"message_id": 99}, "transcript": None},
                       lambda *a: pytest.fail("re-dispatched under a changed reply target"))


# ── transport reality: slow, backgrounded, retried, resent ──────────────────

def test_a_resend_while_the_first_is_still_in_flight_admits_once(tmp_path):
    """A slow link: the phone resends before the first send has been answered."""
    mid = str(uuid4()); entries = [photo()]
    batches.stage(tmp_path, "alpha", "chat", mid, mid, entries)
    payload = {"text": ARABIC, "reply_to": None, "transcript": None}
    calls, impatient = [], []

    def slow(records, paths):
        calls.append(records)
        # Still in flight, still uncommitted as accepted: the resend arrives now.
        impatient.append(batches.submit(
            tmp_path, "chat", mid, mid, payload,
            lambda *a: pytest.fail("the resend dispatched a second turn")))
        return {"result": {"status": "queued"}}

    receipt = batches.submit(tmp_path, "chat", mid, mid, payload, slow)
    assert len(calls) == 1
    assert impatient[0]["replayed"] and impatient[0]["delivery_state"] == "dispatching"
    assert receipt["delivery_state"] == "accepted"


def test_foregrounded_client_resend_after_a_lost_ack_is_one_turn(tmp_path):
    """Backgrounding drops the socket; the receipt is what makes the resend safe."""
    mid = str(uuid4()); entries = [photo(), pdf()]
    batches.stage(tmp_path, "alpha", "chat", mid, mid, entries)
    calls = []
    payload = {"text": ARABIC, "reply_to": None, "transcript": None}
    first = batches.submit(tmp_path, "chat", mid, mid, payload,
                           lambda records, paths: calls.append(records) or
                           {"result": {"status": "queued"}})
    resumed = batches.submit(tmp_path, "chat", mid, mid, payload,
                             lambda *a: pytest.fail("resend dispatched a second turn"))
    assert len(calls) == 1 and resumed["replayed"]
    assert resumed["attachments"] == first["attachments"]
    assert [row["filename"] for row in resumed["attachments"]] == \
        [entry["filename"] for entry in entries]


def test_intentional_identical_resend_is_a_second_distinct_turn(tmp_path):
    """Same photo, same words, sent again on purpose: two turns, two artifact sets."""
    data = iphone_camera_still()
    calls, artifacts = [], []
    for _ in range(2):
        mid = str(uuid4())
        entries = [item(data, "IMG_4821.JPG", "image", "image/jpeg")]
        batches.stage(tmp_path, "alpha", "chat", mid, mid, entries)
        receipt = batches.submit(tmp_path, "chat", mid, mid,
                                 {"text": ARABIC, "reply_to": None, "transcript": None},
                                 lambda records, paths: calls.append(mid) or
                                 {"result": {"status": "queued"}})
        artifacts.append(receipt["attachments"][0]["artifact_id"])
    assert len(calls) == 2 and artifacts[0] != artifacts[1]


def test_stale_local_draft_cannot_change_the_files_behind_one_identity(tmp_path):
    """A draft edited after its identity was minted is refused, not silently swapped."""
    mid = str(uuid4())
    batches.stage(tmp_path, "alpha", "chat", mid, mid, [photo()])
    with pytest.raises(batches.BatchError, match="different attachments"):
        batches.stage(tmp_path, "alpha", "chat", mid, mid, [photo(), pdf()])


# ── a genuinely unsupported item names itself ───────────────────────────────

def test_unreadable_still_names_the_file_and_says_what_to_do(rpc):
    server, session = rpc
    mid = str(uuid4())
    entries = [pdf(), item(heic_still(), "IMG_4822.HEIC", "image", "image/heic")]
    result = submit(server, mid, entries)
    error = result["error"]
    assert error["code"] == 4004
    refusal = error["data"]["attachment_batch"]["refusal"]
    assert refusal["filename"] == "IMG_4822.HEIC"
    assert refusal["item_id"] == entries[1]["item_id"]
    assert refusal["code"] == "image_format_unsupported"
    assert "IMG_4822.HEIC" in error["message"] and "JPEG or PNG" in error["message"]
    assert error["message"] != batches.GENERIC_REFUSAL
    assert "queued_prompt" not in session, "a refused batch must start no turn"


def test_retry_of_a_refused_identity_repeats_the_same_named_reason(rpc):
    """Retry is offered, so it must not decay into "not admitted" on attempt two."""
    server, session = rpc
    mid = str(uuid4())
    entries = [item(heic_still(), "IMG_4822.HEIC", "image", "image/heic")]
    first = submit(server, mid, entries)["error"]
    again = submit(server, mid, entries)["error"]
    assert again["message"] == first["message"] != batches.GENERIC_REFUSAL
    assert again["data"] == first["data"]
    assert again["data"]["attachment_batch"]["retry_with_new_identity"] is True


def test_a_fresh_identity_after_converting_the_photo_goes_through(rpc):
    """The Owner's route back: swap the file the refusal named, send again."""
    server, session = rpc
    refused = str(uuid4())
    submit(server, refused, [item(heic_still(), "IMG_4822.HEIC", "image", "image/heic")])
    fixed = str(uuid4()); entries = [item(plain_png(), "IMG_4822.png", "image", "image/png")]
    assert "error" not in submit(server, fixed, entries)
    one_owner_turn(session, fixed, entries)


# ── the honesty rule the fix must not have loosened ─────────────────────────

@pytest.mark.parametrize("name,mime,data", [
    ("IMG_1.jpg", "image/jpeg", plain_png()),        # PNG wearing a JPEG label
    ("IMG_2.png", "image/png", iphone_camera_still()),
    ("note.png", "image/png", b"#!/bin/sh\necho not-an-image\n"),
    ("empty.png", "image/png", b""),
], ids=["png-as-jpeg", "jpeg-as-png", "script-as-png", "empty-as-png"])
def test_bytes_that_contradict_their_declared_type_are_still_refused(tmp_path, name, mime, data):
    mid = str(uuid4())
    with pytest.raises(batches.BatchError):
        batches.stage(tmp_path, "alpha", "chat", mid, mid, [item(data, name, "image", mime)])
    published = tmp_path / "artifacts" / "published"
    kept = [p.name for p in published.glob("*") if not p.name.startswith("index.db")]
    assert kept == [], "refused bytes must leave no blob behind"


def test_capabilities_advertise_only_types_admission_can_read(rpc):
    server, _ = rpc
    advertised = server._methods["attachments.capabilities"]("c", {})["result"]["image_mime_types"]
    assert "image/jpeg" in advertised and "image/png" in advertised
    # Nothing is advertised that admission would then refuse.
    assert "image/heic" not in advertised and "image/heif" not in advertised
    assert "image/mpo" not in advertised, "the reader's name is not a wire type"


# ── a decoder without a registered wire type is not a licence ───────────────
#
# Pillow 12.3 installs 43 readers and registers a MIME for only 20 of them.
# The 23 unnamed ones (DDS, QOI, WMF, MSP, IM, SPIDER, ...) decode perfectly
# well, so "it decoded" alone would let any of them wear any image label -- and
# the label, not the reader, is what is stored on the durable record and what
# the file route later serves the bytes back as.

def encoded(fmt, mode="RGB", size=(8, 8)):
    """Bytes a Pillow reader with no registered MIME entry will open."""
    stream = BytesIO()
    Image.new(mode, size, (200, 30, 30) if mode == "RGB" else 128).save(stream, fmt)
    return stream.getvalue()


UNNAMED_READERS = ["DDS", "QOI", "IM", "SPIDER"]


@pytest.mark.parametrize("fmt", UNNAMED_READERS)
def test_a_reader_without_a_wire_type_cannot_wear_an_image_label(tmp_path, fmt):
    """These decode. That is not evidence they are the PNG they claim to be."""
    mid = str(uuid4())
    entry = item(encoded(fmt), "pretty.png", "image", "image/png")
    with pytest.raises(batches.BatchError) as caught:
        batches.stage(tmp_path, "alpha", "chat", mid, mid, [entry])
    refusal = batches.refusal_of(caught.value)
    assert refusal["code"] == "image_type_mismatch"
    assert refusal["filename"] == "pretty.png" and refusal["item_id"] == entry["item_id"]
    assert fmt in refusal["reason"], "the Owner is told what the bytes actually are"
    published = tmp_path / "artifacts" / "published"
    assert [p.name for p in published.glob("*") if not p.name.startswith("index.db")] == []


@pytest.mark.parametrize("fmt", UNNAMED_READERS)
def test_a_reader_without_a_wire_type_is_never_admitted_at_all(tmp_path, fmt):
    """Nor under a name invented for it: no such type is advertised as readable."""
    mid = str(uuid4())
    with pytest.raises(batches.BatchError):
        batches.stage(tmp_path, "alpha", "chat", mid, mid,
                      [item(encoded(fmt), f"x.{fmt.lower()}", "image", f"image/{fmt.lower()}")])
    assert f"image/{fmt.lower()}" not in batches.supported_image_mime_types()


def test_no_advertised_image_type_is_one_admission_cannot_corroborate():
    """The capability list and the admission rule read the same table."""
    from PIL import Image as _Image
    _Image.init()
    named = {mime for mime in (batches._reader_wire_mime(reader) for reader in _Image.OPEN)
             if mime and mime.startswith("image/")}
    assert set(batches.supported_image_mime_types()) == named
    # Every reader Pillow leaves unnamed stays unnamed here -- there is no
    # second table quietly inventing a wire type for one of them.
    unnamed = [reader for reader in _Image.OPEN if not _Image.MIME.get(reader)]
    assert unnamed, "this Pillow build is expected to leave some readers unnamed"
    assert all(batches._reader_wire_mime(reader) is None for reader in unnamed
               if reader not in batches._IMAGE_READER_MIME)


# ── an unreadable file of a type the server does support ────────────────────

def test_a_damaged_jpeg_is_not_told_to_resend_itself_as_a_jpeg(tmp_path):
    """"Send it as JPEG" is dead-end advice for a file that already is one."""
    whole = plain_jpeg()
    mid = str(uuid4())
    entry = item(whole[: len(whole) // 2], "half.jpg", "image", "image/jpeg")
    with pytest.raises(batches.BatchError) as caught:
        batches.stage(tmp_path, "alpha", "chat", mid, mid, [entry])
    refusal = batches.refusal_of(caught.value)
    assert refusal["code"] == "image_unreadable"
    assert refusal["filename"] == "half.jpg" and refusal["item_id"] == entry["item_id"]
    assert "Send it as JPEG or PNG" not in refusal["reason"]
    assert "re-export" in refusal["reason"].lower()


def test_a_type_with_no_decoder_still_says_which_types_do_work(tmp_path):
    """The other half of the split: HEIC really cannot be read here."""
    mid = str(uuid4())
    with pytest.raises(batches.BatchError) as caught:
        batches.stage(tmp_path, "alpha", "chat", mid, mid,
                      [item(heic_still(), "IMG.HEIC", "image", "image/heic")])
    refusal = batches.refusal_of(caught.value)
    assert refusal["code"] == "image_format_unsupported"
    assert "JPEG or PNG" in refusal["reason"]


# ── a sealed identity never names a file that is not in the send ────────────

def test_resending_a_refused_identity_with_fixed_files_names_no_stale_file(rpc):
    """The refusal invites "change its files and send again" -- so it must not
    then answer that resend with the name of a file that is no longer there."""
    server, session = rpc
    mid = str(uuid4())
    bad = [item(heic_still(), "IMG_4822.HEIC", "image", "image/heic")]
    first = submit(server, mid, bad)["error"]
    assert first["data"]["attachment_batch"]["refusal"]["filename"] == "IMG_4822.HEIC"

    fixed = [item(plain_png(), "fixed.png", "image", "image/png")]
    again = submit(server, mid, fixed)["error"]
    refusal = again["data"]["attachment_batch"]["refusal"]
    assert "IMG_4822.HEIC" not in again["message"]
    assert refusal["item_id"] is None and refusal["filename"] is None
    assert refusal["code"] == "identity_refused"
    # Still terminal, and now it says the one thing that actually works.
    assert again["data"]["attachment_batch"]["retry_with_new_identity"] is True
    assert "new message" in again["message"]
    assert "queued_prompt" not in session, "a refused identity must start no turn"


def test_a_second_bad_file_under_a_sealed_identity_names_the_second_file(rpc):
    """Re-evaluated, not replayed: the reason describes what was just sent."""
    server, _ = rpc
    mid = str(uuid4())
    submit(server, mid, [item(heic_still(), "IMG_4822.HEIC", "image", "image/heic")])
    other = [item(heic_still(), "IMG_9001.HEIC", "image", "image/heic")]
    error = submit(server, mid, other)["error"]
    refusal = error["data"]["attachment_batch"]["refusal"]
    assert refusal["filename"] == "IMG_9001.HEIC" == error["message"].split('"')[1]
    assert refusal["item_id"] == other[0]["item_id"]
    assert "IMG_4822.HEIC" not in error["message"]


def test_a_submit_on_a_sealed_identity_names_no_file_it_cannot_see(tmp_path):
    """``submit`` carries no items, so it may not repeat a sealed filename."""
    mid = str(uuid4())
    try:
        batches.stage(tmp_path, "alpha", "chat", mid, mid,
                      [item(heic_still(), "IMG_4822.HEIC", "image", "image/heic")])
    except batches.BatchError as exc:
        assert batches.seal_refusal(tmp_path, "chat", mid, mid, batches.refusal_of(exc))
    with pytest.raises(batches.RefusedBatch) as caught:
        batches.submit(tmp_path, "chat", mid, mid, {},
                       lambda *a: pytest.fail("a sealed identity must never dispatch"))
    assert "IMG_4822.HEIC" not in str(caught.value)
    assert caught.value.data["attachment_batch"]["retry_with_new_identity"] is True


def test_the_stored_reason_survives_for_the_resend_that_repeats_the_same_file(tmp_path):
    """The accepted improvement is untouched: same file, same sentence."""
    mid = str(uuid4())
    entries = [item(heic_still(), "IMG_4822.HEIC", "image", "image/heic")]
    reasons = []
    for _ in range(3):
        try:
            batches.stage(tmp_path, "alpha", "chat", mid, mid, entries)
        except batches.BatchError as exc:
            proof = batches.seal_refusal(tmp_path, "chat", mid, mid, batches.refusal_of(exc))
            reasons.append(str(proof or exc))
    assert len(set(reasons)) == 1 and "IMG_4822.HEIC" in reasons[0]
    assert reasons[0] != batches.GENERIC_REFUSAL
