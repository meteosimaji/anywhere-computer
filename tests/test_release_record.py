import pytest

from anywhere_computer.github_releases import ReleaseCandidate
from anywhere_computer.release_record import (
    PreparedRelease,
    read_prepared_release,
    save_prepared_release,
)


def test_record_roundtrip_is_complete_and_leaves_no_staging_files(tmp_path):
    candidate = ReleaseCandidate('v1.2.3', 'a' * 40, 1, 'fixture.zip', 1, 'b' * 64, 2)
    record = PreparedRelease(candidate=candidate, installation=str(tmp_path / 'app'))
    assert read_prepared_release(tmp_path) is None
    save_prepared_release(tmp_path, record)
    assert read_prepared_release(tmp_path) == record
    assert not list(tmp_path.glob('.prepared-release-*'))


@pytest.mark.parametrize('body', [b'{', b'x' * 16385, b'{"version":2}'])
def test_corrupt_record_is_not_treated_as_no_candidate(tmp_path, body):
    (tmp_path / 'prepared-release.json').write_bytes(body)
    with pytest.raises(ValueError):
        read_prepared_release(tmp_path)


def test_failed_replace_preserves_previous_record(tmp_path, monkeypatch):
    candidate = ReleaseCandidate('v1.2.3', 'a' * 40, 1, 'fixture.zip', 1, 'b' * 64, 2)
    record = PreparedRelease(candidate=candidate, installation=str(tmp_path / 'app'))
    save_prepared_release(tmp_path, record)
    def fail(*args):
        raise OSError('fixture replace failure')
    monkeypatch.setattr('anywhere_computer.release_record.os.replace', fail)
    with pytest.raises(OSError, match='replace failure'):
        save_prepared_release(tmp_path, record.model_copy(update={'state': 'applied'}))
    assert read_prepared_release(tmp_path) == record
    assert not list(tmp_path.glob('.prepared-release-*'))
