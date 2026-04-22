import pandas as pd

from backend.engine.core.classifier import classify_category_and_price_group


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
