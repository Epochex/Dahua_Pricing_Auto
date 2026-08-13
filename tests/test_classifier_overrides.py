from pathlib import Path

import pandas as pd
import pytest

from backend.engine.core.classifier import classify_category_and_price_group, detect_series


@pytest.mark.parametrize("product_line", ["Matrix", "AVoIP", "Keyboard", "Decoder"])
def test_sys_central_control_alias_maps_to_keyboard_decoder(product_line):
    sys_row = pd.Series(
        {
            "First Product Line": product_line,
            "Second Product Line": product_line,
            "Catelog Name": product_line,
            "Internal Model": "N70-04H12H",
            "External Model": "N70-04H12H",
        }
    )
    mapping_path = (
        Path(__file__).resolve().parents[1]
        / "mapping"
        / "productline_map_sys_full.csv"
    )
    sys_map = pd.read_csv(mapping_path)

    category, price_group = classify_category_and_price_group(
        None, sys_row, pd.DataFrame(), sys_map
    )

    assert (category, price_group) == ("键盘/解码器", "键盘/解码器")


def test_overseas_project_ptz_is_not_accessory():
    france_row = pd.Series(
        {
            "Internal Model": "DH-PTZ85448-HNF-PA-FL",
            "External Model": "DH-PTZ85448-HNF-PA-FL",
            "Series": "PTZ Cameras for Overseas Project",
            "Description": "48x optical zoom PTZ camera",
            "First Level Product Category": "others",
            "Second Level Product Category": "others",
        }
    )
    sys_row = pd.Series(
        {
            "First Product Line": "Cameras for Overseas Projects",
            "Second Product Line": "PTZ Cameras for Overseas Project",
            "Catelog Name": "Positioning Systems",
            "Internal Model": "DH-PTZ85448-HNF-PA-FL",
            "External Model": "DH-PTZ85448-HNF-PA-FL",
        }
    )

    category, price_group = classify_category_and_price_group(
        france_row, sys_row, pd.DataFrame(), pd.DataFrame()
    )

    assert (category, price_group) == ("PTZ", "PTZ")


def test_dahua_iscan_is_not_accessory():
    france_row = pd.Series(
        {
            "Internal Model": "DHI-ISC-M6040E-BA",
            "External Model": "DHI-ISC-M6040E",
            "First Level Product Category": "others",
            "Second Level Product Category": "others",
        }
    )
    sys_row = pd.Series(
        {
            "First Product Line": "DAHUA ISCAN",
            "Second Product Line": "Luggage and Parcel",
            "Catelog Name": "Baggage Inspection",
            "Internal Model": "DHI-ISC-M6040E-BA",
            "External Model": "DHI-ISC-M6040E",
        }
    )

    category, price_group = classify_category_and_price_group(
        france_row, sys_row, pd.DataFrame(), pd.DataFrame()
    )

    assert (category, price_group) == ("安检机", "安检机")


def test_overseas_distribution_black_ipc_is_ipc_not_accessory():
    sys_row = pd.Series(
        {
            "First Product Line": "Cameras for Overseas Distribution Channels",
            "Second Product Line": "Cameras for Overseas Distribution Channels",
            "Catelog Name": "WizSense 3 Series",
            "Internal Model": "DH-IPC-HDBW3449RP-ZAS-IL-27135-Black",
            "External Model": "DH-IPC-HDBW3449R-ZAS-IL-Black",
        }
    )
    sys_map = pd.DataFrame(
        [
            {
                "priority": 1,
                "field1": "First Product Line",
                "match_type1": "contains",
                "pattern1": None,
                "category": "ACCESSORY",
                "price_group_hint": "ACCESSORY",
            }
        ]
    )

    category, price_group = classify_category_and_price_group(
        None, sys_row, pd.DataFrame(), sys_map
    )

    assert (category, price_group) == ("IPC", "IPC")

    _series_display, series_key = detect_series(None, sys_row, price_group)
    assert series_key == "IPC3"
