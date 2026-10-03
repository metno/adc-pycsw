from pycsw_solr_metno.solr_metno import extract_temporal_extent

def test_extract_temporal_extent_earliest_start_latest_end():
    begin, end = extract_temporal_extent(
        ["2021-01-01", "2019-03-04", "2020-05-01"],
        ["2022-01-01", "2024-12-31", "2023-06-15"],
    )

    assert begin.startswith("2019-03-04")
    assert end.startswith("2024-12-31")


def test_extract_temporal_extent_open_interval_when_more_starts_than_ends():
    begin, end = extract_temporal_extent(
        ["2018-01-01", "2019-01-01"],
        ["2018-12-31"],
    )

    assert begin.startswith("2018-01-01")
    assert end is None


def test_extract_temporal_extent_handles_scalar_values():
    begin, end = extract_temporal_extent("2020-01-01", "2020-12-31")

    assert begin.startswith("2020-01-01")
    assert end.startswith("2020-12-31")
