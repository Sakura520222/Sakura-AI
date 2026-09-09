"""超大 Issue 图片的安全降采样测试 / Oversized issue image safety tests.

覆盖资源策略（优先降采样而非丢弃）：
- 普通 / 4K / 8K / 50MP 量级图片不被新增内存保护丢弃；
- 可解码级降采样（JPEG draft）的超大图成功缩小并进入 vision；
- 无法降采样且估算解码内存超预算的极端图只跳过单张，不影响整体分析；
- Pillow decompression bomb 防护继续生效且同样只跳过单张。

Tests that the oversized-image resource policy downsamples instead of
dropping: normal/4K/8K/50MP-class images survive, draft-able JPEGs shrink at
decode level, non-reducible over-budget images skip individually, and the
Pillow decompression bomb guard still works per-image.
"""

import base64
import io
import random
from contextlib import contextmanager

import httpx
import pytest
from loguru import logger
from PIL import Image

import backend.services.issue_image_service as issue_image_service_module
from backend.services.issue_image_service import collect_issue_images


def _png_bytes(*, size: tuple[int, int] = (8, 8)) -> bytes:
    image = Image.new("RGB", size, (120, 80, 200))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _jpeg_bytes(
    size: tuple[int, int], *, noisy: bool = False, color=(200, 30, 30)
) -> bytes:
    if noisy:
        pixels = random.Random(538).randbytes(size[0] * size[1] * 3)
        image = Image.frombytes("RGB", size, pixels)
    else:
        image = Image.new("RGB", size, color)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=90)
    return output.getvalue()


@contextmanager
def _captured_logs():
    messages: list[str] = []
    handler = logger.add(lambda message: messages.append(str(message)))
    try:
        yield messages
    finally:
        logger.remove(handler)


def _mock_transport_settings(monkeypatch, *, max_size=10_000_000):
    class _Settings:
        issue_vision_max_image_size_bytes = max_size
        issue_vision_allowed_image_domains = (
            "user-images.githubusercontent.com,github.com/user-attachments"
        )

    monkeypatch.setattr(
        issue_image_service_module, "get_settings", lambda: _Settings()
    )


def _mock_http(monkeypatch, handler):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        issue_image_service_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )


# ---------------------------------------------------------------------------
# 解码内存估算 / Decode memory estimation
# ---------------------------------------------------------------------------


def test_estimated_decode_bytes_covers_modes_and_depths():
    estimate = issue_image_service_module._estimated_decode_bytes
    assert estimate("RGB", 1_000_000) == 3_000_000
    assert estimate("RGBA", 1_000_000) == 4_000_000
    assert estimate("L", 1_000_000) == 1_000_000
    assert estimate("P", 1_000_000) == 1_000_000
    assert estimate("CMYK", 1_000_000) == 4_000_000
    assert estimate("I;16", 1_000_000) == 2_000_000


def test_50mp_class_images_stay_within_decode_budget():
    """常见 48MP/50MP 手机照片（任意受支持模式）必须落在完整解码预算内。"""
    budget = issue_image_service_module._OVERSIZED_DECODE_BUDGET_BYTES
    estimate = issue_image_service_module._estimated_decode_bytes
    for mode in ("RGB", "RGBA", "L", "P", "CMYK", "I;16"):
        assert estimate(mode, 50_000_000) <= budget, mode
    # 尺寸上限内的极端情况（7680×7680 RGBA）同样不被预算门拦截
    assert estimate("RGBA", 7680 * 7680) <= budget


# ---------------------------------------------------------------------------
# 正常量级图片不被丢弃 / Ordinary resolutions never dropped
# ---------------------------------------------------------------------------


def test_compress_normal_screenshot_returns_webp_without_downsampling():
    payload = _jpeg_bytes((1920, 1080))
    compressed = issue_image_service_module._compress_image_payload(payload)
    assert compressed is not None
    with Image.open(io.BytesIO(compressed)) as image:
        assert image.format == "WEBP"
        # 普通截图不应被无谓缩小
        assert image.size == (1920, 1080)


def test_compress_4k_noisy_image_converges_within_limits():
    payload = _jpeg_bytes((3840, 2160), noisy=True)
    compressed = issue_image_service_module._compress_image_payload(payload)
    assert compressed is not None
    assert len(compressed) <= issue_image_service_module._IMAGE_OUTPUT_FALLBACK_MAX_BYTES
    with Image.open(io.BytesIO(compressed)) as image:
        assert image.format == "WEBP"
        assert max(image.size) <= issue_image_service_module._IMAGE_MAX_DIMENSION


def test_compress_8k_image_still_reaches_vision_input():
    """8K（7680×4320，约 33MP）不在 draft 档位内，须按预算内全量解码处理。"""
    payload = _jpeg_bytes((7680, 4320))
    with _captured_logs() as messages:
        compressed = issue_image_service_module._compress_image_payload(payload)
    assert compressed is not None
    assert len(compressed) <= issue_image_service_module._IMAGE_OUTPUT_FALLBACK_MAX_BYTES
    with Image.open(io.BytesIO(compressed)) as image:
        assert image.format == "WEBP"
        assert max(image.size) <= issue_image_service_module._IMAGE_MAX_DIMENSION
    # 在预算内正常处理，不应出现"无法安全处理"的跳过日志
    assert not any("cannot be safely decoded" in message for message in messages)


# ---------------------------------------------------------------------------
# 解码级降采样 / Decoder-level downsampling
# ---------------------------------------------------------------------------


def test_compress_downsamples_oversized_jpeg_via_draft_with_log():
    """远超最终尺寸的 JPEG 走 draft 解码级降采样，而不是被丢弃。"""
    payload = _jpeg_bytes((2048, 2048))
    with _captured_logs() as messages:
        compressed = issue_image_service_module._compress_image_payload(
            payload, max_dimension=256
        )
    assert compressed is not None
    with Image.open(io.BytesIO(compressed)) as image:
        assert image.format == "WEBP"
        assert max(image.size) <= 256
    downsampling_logs = [
        message for message in messages if "downsampling before vision" in message
    ]
    assert downsampling_logs, "降采样成功必须有可观测日志"
    assert "original=2048x2048" in downsampling_logs[0]
    assert "processed=256x256" in downsampling_logs[0]


@pytest.mark.asyncio
async def test_collect_downsamples_oversized_jpeg_end_to_end(monkeypatch):
    """端到端：尺寸超限的 JPEG 经 draft 降采样后仍作为多模态输入返回。"""
    _mock_transport_settings(monkeypatch)
    monkeypatch.setattr(issue_image_service_module, "_IMAGE_MAX_DIMENSION", 256)
    payload = _jpeg_bytes((2048, 2048))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "image/jpeg"}, content=payload
        )

    _mock_http(monkeypatch, handler)

    images = await collect_issue_images(
        ["https://user-images.githubusercontent.com/a/huge.jpg"]
    )

    assert len(images) == 1
    assert images[0]["media_type"] == "image/webp"
    decoded = base64.b64decode(images[0]["data"])
    with Image.open(io.BytesIO(decoded)) as image:
        assert image.format == "WEBP"
        assert max(image.size) <= 256


# ---------------------------------------------------------------------------
# 无法安全处理时的单图跳过 / Skip when unsafe, isolate per image
# ---------------------------------------------------------------------------


def test_compress_skips_image_exceeding_decode_budget_with_warning():
    """PNG 无法解码级降采样：估算超预算时跳过并给出明确原因日志。"""
    payload = _png_bytes(size=(256, 256))
    assert issue_image_service_module._estimated_decode_bytes("RGB", 256 * 256) == (
        196_608
    )

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(
            issue_image_service_module, "_OVERSIZED_DECODE_BUDGET_BYTES", 100_000
        )
        with _captured_logs() as messages:
            compressed = issue_image_service_module._compress_image_payload(
                payload, max_dimension=128
            )
    assert compressed is None
    skip_logs = [
        message for message in messages if "cannot be safely decoded" in message
    ]
    assert skip_logs, "超预算跳过必须有可观测日志"
    assert "size=256x256" in skip_logs[0]
    assert "pixels=65536" in skip_logs[0]

    # 预算刚好覆盖估算时不得丢弃（边界为严格大于）
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(
            issue_image_service_module, "_OVERSIZED_DECODE_BUDGET_BYTES", 196_608
        )
        compressed = issue_image_service_module._compress_image_payload(
            payload, max_dimension=128
        )
    assert compressed is not None


@pytest.mark.asyncio
async def test_collect_skips_over_budget_image_and_keeps_other_images(monkeypatch):
    """超预算图只跳过自己；同一 Issue 的其余图片照常进入 vision。"""
    _mock_transport_settings(monkeypatch)
    monkeypatch.setattr(
        issue_image_service_module, "_OVERSIZED_DECODE_BUDGET_BYTES", 1_000_000
    )
    wide = _png_bytes(size=(7900, 200))  # RGB 1.58M 像素 → 估算 ~4.7 MB
    plain = _png_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("wide.png"):
            return httpx.Response(
                200, headers={"content-type": "image/png"}, content=wide
            )
        return httpx.Response(
            200, headers={"content-type": "image/png"}, content=plain
        )

    _mock_http(monkeypatch, handler)

    with _captured_logs() as messages:
        images = await collect_issue_images(
            [
                "https://user-images.githubusercontent.com/a/wide.png",
                "https://user-images.githubusercontent.com/a/plain.png",
            ]
        )

    assert len(images) == 1
    assert images[0]["url"] == "https://user-images.githubusercontent.com/a/plain.png"
    assert any(
        "cannot be safely decoded" in message and "size=7900x200" in message
        for message in messages
    )


@pytest.mark.asyncio
async def test_collect_skips_decompression_bomb_and_keeps_other_images(monkeypatch):
    """Pillow decompression bomb 防护保持不削弱：炸弹图只跳过单张。"""
    _mock_transport_settings(monkeypatch)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 64)
    # 400×300 = 120000 像素 > 2×64，在 Image.open 即抛 DecompressionBombError
    bomb = _png_bytes(size=(400, 300))
    plain = _png_bytes()  # 64 像素，恰好不超过阈值

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("bomb.png"):
            return httpx.Response(
                200, headers={"content-type": "image/png"}, content=bomb
            )
        return httpx.Response(
            200, headers={"content-type": "image/png"}, content=plain
        )

    _mock_http(monkeypatch, handler)

    images = await collect_issue_images(
        [
            "https://user-images.githubusercontent.com/a/bomb.png",
            "https://user-images.githubusercontent.com/a/plain.png",
        ]
    )

    assert len(images) == 1
    assert images[0]["url"] == "https://user-images.githubusercontent.com/a/plain.png"


@pytest.mark.asyncio
async def test_collect_rescues_bomb_warning_zone_jpeg_and_rejects_zone_png(monkeypatch):
    """bomb 警告区（1×–2× MAX_IMAGE_PIXELS）：JPEG 经解码级降档救回，PNG 仍拒绝。"""
    _mock_transport_settings(monkeypatch)
    monkeypatch.setattr(issue_image_service_module, "_IMAGE_MAX_DIMENSION", 256)
    # 2048×2048 = 4.19M 像素：大于 3M（警告）且不超过 6M（硬错误）
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 3_000_000)
    jpeg_payload = _jpeg_bytes((2048, 2048))
    png_payload = _png_bytes(size=(2048, 2048))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("zone.jpg"):
            return httpx.Response(
                200, headers={"content-type": "image/jpeg"}, content=jpeg_payload
            )
        return httpx.Response(
            200, headers={"content-type": "image/png"}, content=png_payload
        )

    _mock_http(monkeypatch, handler)

    with _captured_logs() as messages:
        images = await collect_issue_images(
            [
                "https://user-images.githubusercontent.com/a/zone.jpg",
                "https://user-images.githubusercontent.com/a/zone.png",
            ]
        )

    assert len(images) == 1
    assert images[0]["url"] == "https://user-images.githubusercontent.com/a/zone.jpg"
    assert images[0]["media_type"] == "image/webp"
    decoded = base64.b64decode(images[0]["data"])
    with Image.open(io.BytesIO(decoded)) as image:
        assert image.format == "WEBP"
        assert max(image.size) <= 256
    assert any("downsampling before vision" in message for message in messages)


@pytest.mark.asyncio
async def test_collect_keeps_rejecting_bomb_error_zone_jpeg(monkeypatch):
    """超过 2× MAX_IMAGE_PIXELS 的硬错误区对 JPEG 也不放行。"""
    _mock_transport_settings(monkeypatch)
    # 2048×2048 = 4.19M 像素 > 2×1M → DecompressionBombError
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1_000_000)
    payload = _jpeg_bytes((2048, 2048))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "image/jpeg"}, content=payload
        )

    _mock_http(monkeypatch, handler)

    assert (
        await collect_issue_images(
            ["https://user-images.githubusercontent.com/a/hard-bomb.jpg"]
        )
        == []
    )
