#!/bin/bash
# Creates a zip of the project for uploading to Google Drive.
# Run this on your Mac, then upload the zip to Drive and extract it.
cd "$(dirname "$0")"
zip -r ~/Desktop/studyflow_assistant.zip . \
    -x ".git/*" \
    -x ".browser_data/*" \
    -x "venv/*" \
    -x "__pycache__/*" \
    -x "*/__pycache__/*" \
    -x "*.pyc" \
    -x ".studyflow_run.lock" \
    -x "debug_*.png" \
    -x "ap_session.json"
echo "Created ~/Desktop/studyflow_assistant.zip"
echo "Upload this to Google Drive > My Drive > studyflow_assistant/"
echo "Then open StudyFlow_Colab.ipynb in Colab."
