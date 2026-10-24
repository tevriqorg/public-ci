from transfer_hub.domain import JobState, can_transition, detect_engine, safe_filename


def test_engine_detection():
    assert detect_engine("url", "https://example.com/a.iso") == "aria2"
    assert detect_engine("github", "https://github.com/o/r/releases/download/v/a.zip") == "aria2"
    assert detect_engine("stream", "https://example.com/master.m3u8") == "nm3u8dl"
    assert detect_engine("url", "https://example.com/master.mpd") == "nm3u8dl"
    assert detect_engine("video", "https://example.com/watch/1") == "yt-dlp"
    assert detect_engine("local", None) == "local"


def test_state_transitions():
    assert can_transition(JobState.QUEUED, JobState.FETCHING)
    assert can_transition(JobState.FETCHING, JobState.VERIFYING)
    assert can_transition(JobState.FETCHING, JobState.PAUSED)
    assert can_transition(JobState.PAUSED, JobState.FETCHING)
    assert can_transition(JobState.READY, JobState.DOWNLOADED)
    assert not can_transition(JobState.DONE, JobState.QUEUED)


def test_safe_filename():
    assert safe_filename("../../hello?.zip") == "hello_.zip"


def test_detect_engine_auto():
    from transfer_hub.domain import detect_engine
    assert detect_engine("auto", "https://x.com/user/status/123/video/1") == "yt-dlp"
    assert detect_engine("auto", "https://www.youtube.com/watch?v=abc") == "yt-dlp"
    assert detect_engine("auto", "https://cdn.example.com/master.m3u8?token=x") == "nm3u8dl"
    assert detect_engine("auto", "https://example.com/file.zip") == "aria2"
