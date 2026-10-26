"""The session cookie must only ever go to https://overcast.fm."""

import pytest

from custom_components.overcast.overcast_api import (
    EPISODE_PATH_RE,
    FEED_PATH_RE,
    OvercastInvalidURLError,
    _page_url,
    episode_path_from_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://overcast.fm.evil.com/+x",
        "https://overcast.fm@evil.com/+x",
        "@evil.com/+x",
        "https://user:pw@overcast.fm/+abc",
        "http://overcast.fm/+abc",
        "https://overcast.fm:8443/+abc",
        "https://evil.com/+abc",
        "//evil.com/+abc",
        "https://overcast.fm/podcasts",
        "https://overcast.fm/+abc/../x",
        "https://overcast.fm/+",
        "ftp://overcast.fm/+abc",
        "",
        "overcast.fm/+abc",
    ],
)
def test_rejects_other_hosts_and_paths(url):
    with pytest.raises(OvercastInvalidURLError):
        episode_path_from_url(url)


@pytest.mark.parametrize(
    ("url", "path"),
    [
        ("https://overcast.fm/+d5BN-RlCs", "/+d5BN-RlCs"),
        ("https://OVERCAST.FM/+d5BN-RlCs", "/+d5BN-RlCs"),
        ("https://overcast.fm/+abc_123?t=30", "/+abc_123"),
        ("  https://overcast.fm/+abc  ", "/+abc"),
        ("/+d5BN-RlCs", "/+d5BN-RlCs"),
    ],
)
def test_accepts_overcast_episode_urls(url, path):
    assert episode_path_from_url(url) == path


@pytest.mark.parametrize(
    "path", ["@evil.com/+x", "//evil.com/+x", "/+x/../../", ".evil.com/+x"]
)
def test_page_url_rejects_bad_episode_paths(path):
    with pytest.raises(OvercastInvalidURLError):
        _page_url(path, EPISODE_PATH_RE)


@pytest.mark.parametrize("path", ["//evil.com", "/itunes1/a@b", "@evil.com/p1-x"])
def test_page_url_rejects_bad_feed_paths(path):
    with pytest.raises(OvercastInvalidURLError):
        _page_url(path, FEED_PATH_RE)


def test_page_url_builds_overcast_urls():
    assert _page_url("/+abc", EPISODE_PATH_RE) == "https://overcast.fm/+abc"
    assert _page_url("/p123-AbC", FEED_PATH_RE) == "https://overcast.fm/p123-AbC"
    assert (
        _page_url("/itunes981330533/some-show", FEED_PATH_RE)
        == "https://overcast.fm/itunes981330533/some-show"
    )
