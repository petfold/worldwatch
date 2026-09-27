"""Place names from Natural Earth outlines (presentation only)."""

from worldwatch.config.places import cell_place, place_name


def test_countries_seas_and_oceans():
    assert place_name(35.7, 139.7) == "Japan"
    assert place_name(25.2, 55.3) == "United Arab Emirates"
    assert place_name(26.5, 52.0) == "Persian Gulf, off Qatar"
    assert place_name(-21.3, 167.9) == "Coral Sea, off New Caledonia"
    assert place_name(0.0, -140.0).endswith("Pacific Ocean")
    assert place_name(51.4, 30.1) == "Ukraine"


def test_non_spatial_cells_have_no_place():
    assert cell_place("GLOBAL") == "" and cell_place("_PRESENCE_") == ""
