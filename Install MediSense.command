#!/bin/bash
# Double-click to install MediSense. (If macOS says it can't be opened,
# right-click this file, choose Open, then Open again.)
cd "$(dirname "$0")" && bash ./install.sh
echo
read -n 1 -s -r -p "Press any key to close this window."
