name: F4P Equities Weekly Update

# NOTE: schedule trigger removed 2026-08-27. F4P Equities Master Weekly
# Update now owns the Saturday schedule and runs this step as part of
# a chained sequence. Running both on a schedule would double-fetch
# from Alpha Vantage and double-bill the Anthropic API every week.
# This workflow stays available for manual runs (testing, re-running
# just this step) via workflow_dispatch.
on:
  workflow_dispatch:

jobs:
  update:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - run: pip install gspread google-auth requests

      - run: python f4p_equities_weekly_update.py
        env:
          GOOGLE_CREDENTIALS: ${{ secrets.GOOGLE_CREDENTIALS }}
          EQUITIES_SHEET_ID: ${{ secrets.EQUITIES_SHEET_ID }}
          ALPHA_VANTAGE_API_KEY: ${{ secrets.ALPHA_VANTAGE_API_KEY }}

    
