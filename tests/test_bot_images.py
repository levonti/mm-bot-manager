"""Image commands, hostile uploads and binary Mattermost API contracts."""

import json
from dataclasses import replace
from io import BytesIO
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from PIL import Image

from mm_bot_manager.commands import dispatch
from mm_bot_manager.dispatcher import handle_event
from mm_bot_manager.i18n import UserFacingError
from mm_bot_manager.images import AVATAR_LIMIT, normalize_image
from mm_bot_manager.mm_client import MattermostClient, MattermostError
from mm_bot_manager.updates import bot_mutation
from tests.test_commands import ctx as command_context
from tests.test_hardening import create_bot, event
from tests.test_hardening import manager as event_manager

ctx = command_context
manager = event_manager

FILE = "f" * 26
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M0 0 L24 24"/></svg>'


def png(size=(32, 16), fmt="PNG"):
    out = BytesIO()
    Image.new("RGB", size, "red").save(out, format=fmt)
    return out.getvalue()


def uploads(mm, caller="alice-id", data=None):
    if data is None:
        data = png()
    mm.get_file_info = AsyncMock(
        return_value={
            "id": FILE,
            "user_id": caller,
            "post_id": "post-1",
            "size": len(data),
        }
    )
    mm.download_image = AsyncMock(return_value=data)
    mm.set_profile_image = AsyncMock()
    mm.reset_profile_image = AsyncMock()


async def setup(ctx, data=None):
    make, mm, *_ = ctx
    context = replace(make(), post_id="post-1", file_ids=(FILE,))
    await dispatch(
        context, "!create demo-bot --repo https://git.example/team/project --criticality 2"
    )
    uploads(mm, data=data)
    return context, mm


@pytest.mark.parametrize("kind", ["avatar", "icon"])
async def test_set_and_reset(ctx, kind):
    context, mm = await setup(ctx, png())
    assert "completed" in await dispatch(context, f"!{kind} set demo-bot")
    mm.set_profile_image.assert_awaited_once()
    assert mm.set_profile_image.call_args.args[:1] == ("bot-0",)
    assert "completed" in await dispatch(replace(context, file_ids=()), f"!{kind} reset demo-bot")
    mm.reset_profile_image.assert_awaited_once_with("bot-0")


@pytest.mark.parametrize(
    "change",
    [
        {"caller_user_id": "other"},
        {"channel_type": "O"},
        {"channel_type": None},
        {"file_ids": ()},
        {"file_ids": (FILE, FILE)},
        {"file_ids": ("../bad",)},
        {"post_id": ""},
    ],
)
async def test_reject_context_without_writes(ctx, change):
    context, mm = await setup(ctx)
    assert "completed" not in await dispatch(replace(context, **change), "!icon set demo-bot")
    mm.set_profile_image.assert_not_awaited()


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "other"),
        ("user_id", "other"),
        ("post_id", "other"),
        ("size", AVATAR_LIMIT + 1),
        ("size", 0),
        ("size", "10"),
        ("size", True),
    ],
)
async def test_reject_file_metadata_before_download(ctx, field, value):
    context, mm = await setup(ctx)
    mm.get_file_info.return_value[field] = value
    assert "completed" not in await dispatch(context, "!icon set demo-bot")
    mm.download_image.assert_not_awaited()
    mm.set_profile_image.assert_not_awaited()


@pytest.mark.parametrize(
    "command",
    [
        "!icon",
        "!icon delete demo-bot",
        "!icon set demo-bot extra",
        "!icon set demo-bot --url https://example.org",
        "!icon set missing",
        "!icon reset demo-bot",
    ],
)
async def test_bad_commands(ctx, command):
    context, mm = await setup(ctx)
    assert "completed" not in await dispatch(context, command)
    mm.set_profile_image.assert_not_awaited()
    mm.reset_profile_image.assert_not_awaited()


async def test_serialization(ctx):
    context, mm = await setup(ctx)
    async with bot_mutation(context, "bot-0"):
        assert "completed" not in await dispatch(context, "!icon set demo-bot")
    mm.set_profile_image.assert_not_awaited()


@pytest.mark.parametrize(
    "stage", ["get_file_info", "download_image", "set_profile_image", "reset_profile_image"]
)
@pytest.mark.parametrize(
    "failure",
    [httpx.ReadTimeout("secret"), MattermostError(403, "secret"), MattermostError(500, "secret")],
)
async def test_network_failures(ctx, stage, failure):
    context, mm = await setup(ctx)
    getattr(mm, stage).side_effect = failure
    reset = stage == "reset_profile_image"
    reply = await dispatch(
        replace(context, file_ids=() if reset else (FILE,)),
        f"!icon {'reset' if reset else 'set'} demo-bot",
    )
    assert "completed" not in reply and "secret" not in reply
    if stage in {"set_profile_image", "reset_profile_image"} and (
        isinstance(failure, httpx.ReadTimeout) or failure.status == 500
    ):
        assert "uncertain" in reply


async def test_real_event_passes_attachment_and_is_deduplicated(manager):
    m = manager
    await create_bot(m)
    uploads(m.mm, caller="alice")
    ev = event(message="!icon set demo-bot")
    post = json.loads(ev["data"]["post"])
    post["file_ids"] = [FILE]
    ev["data"]["post"] = json.dumps(post)
    await handle_event(ev, m.router, m.factory)
    await handle_event(ev, m.router, m.factory)
    m.mm.set_profile_image.assert_awaited_once()
    assert "completed" in m.mm.posts[-1]["message"]


@pytest.mark.parametrize(
    "data", [b"", b"fake.png", SVG, png((4097, 1)), png(fmt="GIF"), b"0" * (AVATAR_LIMIT + 1)]
)
def test_invalid_avatar(data):
    with pytest.raises(UserFacingError):
        normalize_image(data)


@pytest.mark.parametrize("fmt", ["PNG", "JPEG"])
def test_avatar_normalization(fmt):
    data = normalize_image(png(fmt=fmt))
    with Image.open(BytesIO(data)) as image:
        assert image.size == (512, 512)
        assert image.format == "PNG"
        assert not image.info


@respx.mock
async def test_binary_api_and_download_limit():
    async with MattermostClient("https://mm.example/api/v4", "token") as mm:
        respx.get(f"https://mm.example/api/v4/files/{FILE}").respond(200, content=SVG)
        assert await mm.download_image(FILE, len(SVG)) == SVG
        with pytest.raises(UserFacingError):
            await mm.download_image(FILE, 1)
        path = "/users/b/image"
        route = respx.post("https://mm.example/api/v4" + path).respond(200, json={"status": "OK"})
        await mm.set_profile_image("b", SVG)
        request = route.calls.last.request
        assert "multipart/form-data" in request.headers["content-type"]
        assert b'name="image"' in request.content
        assert SVG in request.content
        reset = respx.delete("https://mm.example/api/v4" + path).respond(204)
        await mm.reset_profile_image("b")
        assert reset.called
        respx.get(f"https://mm.example/api/v4/files/{FILE}/info").respond(200, json={"id": FILE})
        assert (await mm.get_file_info(FILE))["id"] == FILE
        respx.get(f"https://mm.example/api/v4/files/{FILE}").respond(403, content=b"secret")
        with pytest.raises(MattermostError) as caught:
            await mm.download_image(FILE, 10)
        assert caught.value.status == 403
        assert "secret" not in str(caught.value)


@pytest.mark.parametrize("kind,resource", [("bot_create", "demo-bot"), ("token_create", "bot-0")])
async def test_unfinished_operation_blocks_image_change(ctx, kind, resource):
    context, mm = await setup(ctx)
    context.operation_repo.start(kind, resource)
    assert "unfinished" in await dispatch(context, "!icon set demo-bot")
    mm.get_file_info.assert_not_awaited()
    mm.set_profile_image.assert_not_awaited()


async def test_corrupt_download_is_never_uploaded(ctx):
    context, mm = await setup(ctx, b"not PNG despite filename")
    assert "Invalid image" in await dispatch(context, "!icon set demo-bot")
    mm.set_profile_image.assert_not_awaited()


@pytest.mark.parametrize("attachments", [None, "not-a-list", [1], ["../file"]])
async def test_malformed_event_attachment_is_rejected(manager, attachments):
    m = manager
    await create_bot(m)
    uploads(m.mm, caller="alice")
    ev = event(message="!icon set demo-bot")
    post = json.loads(ev["data"]["post"])
    post["file_ids"] = attachments
    ev["data"]["post"] = json.dumps(post)
    await handle_event(ev, m.router, m.factory)
    m.mm.get_file_info.assert_not_awaited()
    m.mm.set_profile_image.assert_not_awaited()


def test_animated_png_is_rejected():
    out = BytesIO()
    Image.new("RGB", (2, 2), "red").save(
        out, format="PNG", save_all=True, append_images=[Image.new("RGB", (2, 2), "blue")]
    )
    with pytest.raises(UserFacingError):
        normalize_image(out.getvalue())


def test_source_metadata_is_removed_and_orientation_applied():
    source = Image.new("RGB", (100, 100), "red")
    source.paste("blue", (50, 0, 100, 100))
    exif = Image.Exif()
    exif[274] = 3  # Rotate 180 degrees.
    exif[270] = "private-source-metadata"
    out = BytesIO()
    source.save(out, format="JPEG", exif=exif)
    normalized = normalize_image(out.getvalue())
    assert b"private-source-metadata" not in normalized
    with Image.open(BytesIO(normalized)) as result:
        assert not result.info and not result.getexif()
        assert result.getpixel((20, 20))[2] > 200  # Blue moved from right to left.
