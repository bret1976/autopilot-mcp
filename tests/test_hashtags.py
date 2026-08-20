from app.hashtags import apply_hashtags, hashtags_for
from app.config import LOCKED_HASHTAGS


def test_instagram_includes_locked_and_stays_in_cap() -> None:
    tags = hashtags_for("instagram", ["Kling", "NightDrive", "StreetLamp", "Anamorphic"])
    assert list(LOCKED_HASHTAGS) == tags[:3]
    assert 8 <= len(tags) <= 12
    assert "#Kling" in tags


def test_linkedin_and_facebook_caps() -> None:
    for platform in ("linkedin", "facebook"):
        tags = hashtags_for(platform, ["Kling", "NightDrive", "Street", "Cut", "Lamp", "Grain"])
        assert 5 <= len(tags) <= 8
        assert "#6FrameStudio" in tags


def test_twitter_one_or_two_and_under_280() -> None:
    tags = hashtags_for("twitter", ["Kling", "NightDrive", "TooMany"])
    assert 1 <= len(tags) <= 2
    body = "The original. Cut under 60. Posted."
    out = apply_hashtags("x", body, ["Kling"])
    assert len(out) <= 280
    assert out.endswith(tuple(tags)) or any(tag in out for tag in tags)


def test_gbp_has_a_cap_and_does_not_raise() -> None:
    tags = hashtags_for("GBP", ["Kling", "NightDrive", "TooMany", "Extra"])
    assert len(tags) <= 3
    out = apply_hashtags("google_business", "Sunday hours are up.", ["Kling"])
    assert "Sunday hours" in out
    unknown = hashtags_for("threads", ["One"])
    assert len(unknown) <= 3


def test_youtube_hashtags_applied_to_caption() -> None:
    out = apply_hashtags("youtube", "Cut from the viral original.", ["Kling"])
    assert "#6FrameStudio" in out
    assert "#AIFilmmaking" in out
    assert "#AICinema" in out
    assert "#Kling" in out
