import json

from feishu_link.sender import build_media_content


def test_build_media_content_without_cover() -> None:
    assert json.loads(build_media_content("file_xxx")) == {"file_key": "file_xxx"}


def test_build_media_content_with_cover() -> None:
    assert json.loads(build_media_content("file_xxx", "img_xxx")) == {
        "file_key": "file_xxx",
        "image_key": "img_xxx",
    }
