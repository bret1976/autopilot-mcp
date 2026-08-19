from app.platforms import aspect_for, split_batches, youtube_title
from app.config import LANDSCAPE_PLATFORMS, VERTICAL_PLATFORMS


def test_vertical_and_landscape_sets() -> None:
    assert VERTICAL_PLATFORMS == frozenset({"instagram", "tiktok", "youtube", "facebook"})
    assert LANDSCAPE_PLATFORMS == frozenset({"linkedin", "twitter", "x"})
    for name in VERTICAL_PLATFORMS:
        assert aspect_for(name) == "9:16"
    for name in ("linkedin", "twitter", "x"):
        assert aspect_for(name) == "16:9"


def test_split_batches() -> None:
    batches = split_batches(
        ["Instagram", "TikTok", "YouTube Shorts", "Facebook", "LinkedIn", "X"]
    )
    assert batches["vertical_9x16"] == ["instagram", "tiktok", "youtube", "facebook"]
    assert batches["landscape_16x9"] == ["linkedin", "twitter"]
    assert batches["unknown"] == []


def test_youtube_title_gets_shorts() -> None:
    assert youtube_title("Night tungsten") == "Night tungsten #Shorts"
    assert "#Shorts" in youtube_title("Already tagged #Shorts")
